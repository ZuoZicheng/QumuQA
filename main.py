import os
#os.environ['CUDA_VISIBLE_DEVICES']='0,1'
import csv
import codecs
import matplotlib.pyplot as plt
import pylab as pl
import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
import pdb
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data.dataloader import default_collate
import warnings
from pprint import pprint
import json

# self-defined
import model.fusion_net as fusion_net
import model.answer_net as answer_net
from model import Vector, SimpleClassifier
from config import cfg
from torchlight import initialize_exp, set_seed, snapshot, get_dump_path, show_params
from utils import unseen_mask, freeze_layer, cosine_sim, Metrics, instance_bce_with_logits
from data import fvqa
import copy
# torch.multiprocessing.set_start_method('spawn')

warnings.filterwarnings('ignore')


class Runner:
    def __init__(self, args):
        # prepare for: data , model, loss fuction, optimizer

        self.log_dir = get_dump_path(args)
        self.model_dir = os.path.join(self.log_dir, 'model')

        self.word2vec = Vector(args.FVQA.common_data_path)
        # data load
        self.train_loader = fvqa.get_loader(args, self.word2vec, train=True)
        self.val_loader = fvqa.get_loader(args, self.word2vec, val=True)

        self.avocab = default_collate(list(range(0, args.FVQA.max_ans)))
        # self.avocab_fact = default_collate(list(range(0, 2791)))
        # self.avocab_relation = default_collate(list(range(0, 103)))

        # question_word2vec: get the word vector (for each word in question )
        # the id of which could map to the vector of corresponding token
        self.question_word2vec = self.word2vec._prepare(self.train_loader.dataset.token_to_index)

        # get the fusion_model and answer_net
        self._model_choice(args)
        if args.exp_name == 'semantic_space' or args.exp_name == 'Zsl_semantic_space':
            self.fusion_model_head = self.head_model(args)
        elif args.exp_name == 'knowledge_space' or args.exp_name == 'Zsl_knowledge_space':
            self.fusion_model_head = self.head_model(args)
            self.fusion_model_rel = self.rel_model(args)

        # get the mask from zsl
        self.negtive_mux = unseen_mask(args, self.val_loader)

        # optimizer
        params_for_optimization = list(self.fusion_model.parameters()) + list(self.answer_net.parameters())
        self.optimizer = optim.Adam([p for p in params_for_optimization if p.requires_grad], lr=args.TRAIN.lr)

        # loss fuction
        self.log_softmax = nn.LogSoftmax(dim=1).cuda()

        # Recorder
        self.max_acc = [0, 0, 0, 0]
        self.max_zsl_acc = [0, 0, 0, 0]
        self.best_epoch = 0
        self.correspond_loss = 1e20

        self.early_stop = 0

        print("fusion_model:")
        pprint(self.fusion_model)
        print("Answer Model:")
        pprint(self.answer_net)

        self.args = args

        # test stage:
        if self.args.now_test:
            print("begin test! ...")
            print("loading model  ...")
            self._load_model(self.fusion_model, "fusion")
            self._load_model(self.answer_net, "embedding")
        if self.args.exp_name == 'knowledge_space' or args.exp_name == 'Zsl_knowledge_space':
            self._load_saved_model(self.fusion_model_head, "fusion", "fact")
            self._load_saved_model(self.fusion_model_rel, "fusion", "relation")
        elif self.args.exp_name == 'semantic_space' or args.exp_name == 'Zsl_semantic_space':
            self._load_saved_model(self.fusion_model_head, "fusion", "fact")

    def run(self):
        # 1. define the parameters which are out the epoch
        # 2. Update statistical indicator
        # 3. concate of answer embedding
        tmp_args = self.args
        self.train_loader_fact = fvqa.get_loader(tmp_args, self.word2vec, train=True)
        self.train_loader_relation = fvqa.get_loader(tmp_args, self.word2vec, train=True)

        self.g_head = torch.from_numpy(np.loadtxt('headlist.txt', dtype=np.float32, delimiter=',').reshape((2791, 1024))).cuda()
        self.g_rel = torch.from_numpy(np.loadtxt('rellist.txt', dtype=np.float32, delimiter=',').reshape((103, 1024))).cuda()
        self.g_tail = torch.from_numpy(np.loadtxt('anslist.txt', dtype=np.float32, delimiter=',').reshape((500, 300))).cuda()

        # Answer embedding :
        # choices belong to: ['CLS', 'W2V', 'KG', 'GAE', 'KG_W2V', 'KG_GAE', 'GAE_W2V', 'KG_GAE_W2V']
        # well, we recommend only use the parameter : 'CLS' or 'W2V'.
        # since that the resource of other choices need extra training.
        if args.method_choice != 'CLS':    #method_choice == 'W2V'
            previous_var = None
            for method_choice in self.method_list:
                # get the corresponding choice embedding
                answer_var, answer_len = self.train_loader.dataset._get_answer_vectors(method_choice, self.avocab)

                # answer_var_fact, _ = self.train_loader_fact.dataset._get_answer_vectors(method_choice, self.avocab_fact)
                # answer_var_relation, _ = self.train_loader_relation.dataset._get_answer_vectors(method_choice, self.avocab_relation)
                # answer_var_fact = F.normalize(answer_var_fact, p=2, dim=1)
                # answer_var_relation = F.normalize(answer_var_relation, p=2, dim=1)

                # normalize in row and then concate then
                answer_var = F.normalize(answer_var, p=2, dim=1)
                if previous_var is not None:
                    previous_var = torch.cat([previous_var, answer_var], dim=1)
                else:
                    previous_var = answer_var
            self.answer_var = Variable(previous_var.float()).cuda()
            # self.answer_var_fact = Variable(answer_var_fact.float()).cuda()
            # self.answer_var_relation = Variable(answer_var_relation.float()).cuda()

        # warm up (ref: ramen)
        self.gradual_warmup_steps = [i * self.args.TRAIN.lr for i in torch.linspace(0.5, 2.0, 7)]
        self.lr_decay_epochs = range(14, 47, self.args.TRAIN.lr_decay_step)

        # if test:
        if self.args.now_test:
            self.args.TRAIN.epochs = 2

        train_loss_matrix = []
        train_lr_matrix = []
        train_acc_matrix = []
        val_loss_matrix = []
        val_acc_matrix = []

        for epoch in range(self.args.TRAIN.epochs):

            self.early_stop += 1
            if self.args.patience < self.early_stop:
                # early stop
                break
            # warm up
            if epoch < len(self.gradual_warmup_steps):
                self.optimizer.param_groups[0]['lr'] = self.gradual_warmup_steps[epoch]
            elif epoch in self.lr_decay_epochs:
                self.optimizer.param_groups[0]['lr'] *= self.args.TRAIN.lr_decay_rate

            self.train_metrics = Metrics()
            self.val_metrics = Metrics()
            self.zsl_metrics = Metrics()
            # use TOP50 metrics for fact mapping:
            if self.args.fact_map == 1:
                self.train_metrics = Metrics(topnum=50)
                self.val_metrics = Metrics(topnum=50)
                self.zsl_metrics = Metrics(topnum=50)

            # train
            if not self.args.now_test:
                ######## begin training!! #######
                self.train(epoch)
                #################################
                lr = self.optimizer.param_groups[0]['lr']
                # recode:
                logger.info(
                    f'Train Epoch {epoch}: LOSS={self.train_metrics.total_loss: .5f}, lr={lr: .6f}, acc1={self.train_metrics.acc_1: .2f},acc3={self.train_metrics.acc_3: .2f},acc10={self.train_metrics.acc_10: .2f}')

                #train_loss = self.train_metrics.total_loss.cpu().detach().numpy().tolist()
                train_loss_matrix.append(self.train_metrics.total_loss.cpu().detach().numpy().tolist())
                #train_lr = lr.cpu().numpy().tolist()
                train_lr_matrix.append(lr.cpu().numpy().tolist())
                #train_acc = self.train_metrics.acc_1
                train_acc_matrix.append(self.train_metrics.acc_1)

            # eval
            if epoch % 1 == 0 and epoch > 0:
                ######## begin evaling!! #######
                self.eval(epoch)
                #################################
                logger.info('#################################################################################################################')
                logger.info(f'Test Epoch {epoch}: LOSS={self.val_metrics.total_loss: .5f}, acc1={self.val_metrics.acc_1: .2f}, acc3={self.val_metrics.acc_3: .2f}, acc10={self.val_metrics.acc_10: .2f}')

                #val_loss = self.val_metrics.total_loss.cpu().detach().numpy().tolist()
                val_loss_matrix.append(self.val_metrics.total_loss.cpu().detach().numpy().tolist())
                #val_acc = self.val_metrics.acc_1
                val_acc_matrix.append(self.val_metrics.acc_1)

                if args.ZSL and not self.args.fact_map and not args.relation_map:
                    logger.info(f'Zsl Epoch {epoch}: LOSS={self.zsl_metrics.total_loss: .5f}, acc1={self.zsl_metrics.acc_1: .2f}, acc3={self.zsl_metrics.acc_3: .2f}, acc10={self.zsl_metrics.acc_10: .2f}')
                logger.info('#################################################################################################################')

                # add 0.1 accuracy punishment, avoid for too much attention on hit@10 acc
                # 添加0.1的精读惩罚, 防止模型过多的关注hit@10 acc
                if self.val_metrics.total_loss < (self.correspond_loss - 1) or self.val_metrics.acc_all > (self.max_acc[3] + 0.2):
                    # reset early_stop and updata
                    self.early_stop = 0
                    self.best_epoch = epoch
                    self.correspond_loss = self.val_metrics.total_loss
                    self._updata_best_result(self.max_acc, self.val_metrics)

                    self.best_fusion_model = copy.deepcopy(self.fusion_model)
                    self.best_answer_net = copy.deepcopy(self.answer_net)

                    # ZSL result
                    if args.ZSL and not self.args.fact_map and not args.relation_map:
                        self._updata_best_result(self.max_zsl_acc, self.zsl_metrics)

                if not args.no_tensorboard and not self.args.now_test:
                    writer.add_scalar('loss', self.val_metrics.total_loss, epoch)
                    writer.add_scalar('acc1', self.val_metrics.acc_1, epoch)
                    writer.add_scalar('acc3', self.val_metrics.acc_3, epoch)
                    writer.add_scalar('acc10', self.val_metrics.acc_10, epoch)

        # save the model
        if not self.args.now_test and self.args.save_model:
            self.fusion_model_path = self._save_model(self.best_fusion_model, "fusion")
            self.answer_net_path = self._save_model(self.best_answer_net, "embedding")
            #self.draw_train(train_loss_matrix, train_lr_matrix, train_acc_matrix)
            #self.draw_val(val_loss_matrix, val_acc_matrix)
            self.write_exp(val_loss_matrix, val_acc_matrix)
            print('exp_data write success')

    def write_exp(self, val_loss, val_acc):
        # valloss = open('val_loss.txt','w')
        # for val in val_loss:
        #     valloss.write(str(val)+'\n')
        # valloss.close()
        if self.args.exp_name == 'object_space':
            valacc = open('val_head_acc.txt', 'w')
            for ac in val_acc:
                valacc.write(str(ac) + '\n')
            valacc.close()
        elif self.args.exp_name == 'semantic_space':
            valacc = open('val_pred_acc.txt', 'w')
            for ac in val_acc:
                valacc.write(str(ac) + '\n')
            valacc.close()
        else:
            valacc = open('val_tail_acc.txt', 'w')
            for ac in val_acc:
                valacc.write(str(ac) + '\n')
            valacc.close()
        return None

    # def draw_train(self,train_loss, train_lr, train_acc):
    #     train_x = []
    #     for axis in range(0,len(train_loss)):
    #         train_x.append(axis)
    #     #fig = plt.figure(figsize=(7,5))
    #     pl.plot(train_x,train_loss,'g-',label=u'train_loss')
    #     pl.plot(train_x,train_acc,'r-',label=u'train_acc')
    #     pl.plot(train_x,train_lr,'b-',label=u'train_lr')
    #     pl.legend()
    #     pl.xlabel(u'epoch')
    #     plt.title('Training Curve')
    #     plt.savefig('train_curve')
    #     return None
    # def draw_val(self,val_loss, val_acc):
    #     val_x = []
    #     for axis in range(0,len(val_loss)):
    #         val_x.append(axis)
    #     #fig = plt.figure(figsize=(7,5))
    #     pl.plot(val_x,val_loss,'g-',label=u'val_loss')
    #     pl.plot(val_x,val_acc,'r-',label=u'val_acc')
    #     pl.legend()
    #     pl.xlabel(u'epoch')
    #     plt.title('Validation Curve')
    #     plt.savefig('val_curve')
    #     return None


    # def findkb(self,tq):
    #     qa_dict = tq.iterable.dataset.qa_json
    #     #kblist = []
    #     dbpedia = 0
    #     conceptnet = 0
    #     webchild = 0
    #     for item in qa_dict:
    #         kbname = qa_dict[item]['kb_source']
    #         #kblist.append(kbname)
    #         if kbname == 'dbpedia':
    #             dbpedia += 1
    #         elif kbname == 'conceptnet':
    #             conceptnet += 1
    #         elif kbname == 'webchild':
    #             webchild += 1
    #         else:
    #             print('error')
    #     #kblist = set(kblist)
    #     return dbpedia,conceptnet,webchild

    def _get_fact_relation_dict(self):
        with open(self.args.FVQA.fact_relation_to_ans_path, 'r') as fd:
            fact_relation_to_ans = json.load(fd)
        return fact_relation_to_ans

    def train(self, epoch):
        #self.fusion_model = torch.nn.DataParallel(self.fusion_model)
        self.fusion_model.train()
        self.answer_net.train()

        prefix = "train"
        tq = tqdm(self.train_loader, desc='{} E{:03d}'.format(prefix, epoch), ncols=0)
        fact_relation_to_ans = self._get_fact_relation_dict()
        #kblist = self.findkb(tq)

        for visual_features, boxes, question_features, answers, idx, q_len in tq:
            visual_features = Variable(visual_features.float()).cuda()
            boxes = Variable(boxes.float()).cuda()
            question_features = Variable(question_features).cuda()
            answers = Variable(answers).cuda()
            q_len = Variable(q_len).cuda()

            if self.args.exp_name == 'semantic_space'or self.args.exp_name == 'Zsl_semantic_space':
                fusion_embedading_head = self.fusion_model_head(visual_features, boxes, question_features, q_len)
                fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len,
                                                      fusion_embedading_head)
            elif self.args.exp_name == 'knowledge_space' or self.args.exp_name == 'Zsl_knowledge_space':
                fusion_embedading_head = self.fusion_model_head(visual_features, boxes, question_features, q_len)
                fusion_embedading_rel = self.fusion_model_rel(visual_features, boxes, question_features, q_len,
                                                              fusion_embedading_head)
                fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len,
                                                      fusion_embedading_head, fusion_embedading_rel,
                                                      fact_relation_to_ans, self.g_head, self.g_rel, self.g_tail)
            else:
                fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len)

            # Classifier-based methods
            if args.method_choice == 'CLS':
                # TODO: Normalization?
                predicts = self.answer_net(fusion_embedading)
                loss = instance_bce_with_logits(predicts, answers / 10)
            # Mapping-based methods
            else:
                answer_embedding = self.answer_net(self.answer_var)     #ans(500,300) -> (500,1024)
                                                                        #fact(2791,300) -> (2791,1024)
                # notice the temperature (correspoding to specific score)
                predicts = cosine_sim(fusion_embedading, answer_embedding) / self.args.loss_temperature
                predicts = predicts.to(torch.float64)
                nll = -self.log_softmax(predicts).to(torch.float64)
                # loss = (nll * answers[0] / answers[0].sum(1, keepdim=True)).sum(dim=1).mean()
                loss = (nll * answers / answers.sum(1, keepdim=True)).sum(dim=1).mean()

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            self.train_metrics.update_per_batch(loss, answers.data, predicts.data)
        self.train_metrics.update_per_epoch()

    def eval(self, epoch):
        #self.fusion_model = torch.nn.DataParallel(self.fusion_model)
        self.fusion_model.eval()
        self.answer_net.eval()
        prefix = "eval"
        tq = tqdm(self.val_loader, desc='{} E{:03d}'.format(prefix, epoch), ncols=0)
        fact_relation_to_ans = self._get_fact_relation_dict()

        for visual_features, boxes, question_features, answers, idx, q_len in tq:
            with torch.no_grad():
                visual_features = Variable(visual_features.float()).cuda()
                boxes = Variable(boxes.float()).cuda()
                question_features = Variable(question_features).cuda()
                answers = Variable(answers).cuda()
                q_len = Variable(q_len).cuda()
                if self.args.exp_name == 'semantic_space' or self.args.exp_name == 'Zsl_semantic_space':
                    fusion_embedading_head = self.fusion_model_head(visual_features, boxes, question_features, q_len)
                    fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len,
                                                          fusion_embedading_head)
                elif self.args.exp_name == 'knowledge_space' or self.args.exp_name == 'Zsl_knowledge_space':
                    fusion_embedading_head = self.fusion_model_head(visual_features, boxes, question_features, q_len)
                    fusion_embedading_rel = self.fusion_model_rel(visual_features, boxes, question_features, q_len,
                                                                  fusion_embedading_head)
                    fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len,
                                                          fusion_embedading_head, fusion_embedading_rel,
                                                          fact_relation_to_ans, self.g_head, self.g_rel, self.g_tail)
                else:
                    fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len)
                #fusion_embedading = self.fusion_model(visual_features, boxes, question_features, q_len)

                if args.method_choice == 'CLS':
                    predicts = self.answer_net(fusion_embedading)
                    loss = instance_bce_with_logits(predicts, answers / 10)

                else:
                    answer_embedding = self.answer_net(self.answer_var)
                    predicts = cosine_sim(fusion_embedading, answer_embedding) / self.args.loss_temperature
                    predicts = predicts.to(torch.float64)
                    nll = -self.log_softmax(predicts).to(torch.float64)
                    loss = (nll * answers / answers.sum(1, keepdim=True)).sum(dim=1).mean()

                if args.ZSL == 1 and not self.args.fact_map and not args.relation_map:
                    # if predicts.shape[0] != self.negtive_mux.shape[0]:
                    #     pdb.set_trace()
                    zsl_predicts = predicts + self.negtive_mux[:predicts.shape[0], :]

            self.val_metrics.update_per_batch(loss, answers.data, predicts.data)
            if args.ZSL == 1 and not self.args.fact_map and not args.relation_map:
                self.zsl_metrics.update_per_batch(loss, answers.data, zsl_predicts.data)

        self.val_metrics.update_per_epoch()
        if args.ZSL == 1 and not self.args.fact_map and not args.relation_map:
            self.zsl_metrics.update_per_epoch()

    def head_model(self, args):
        # models api
        fusion_model = getattr(fusion_net, 'SAN')(args, self.train_loader.dataset,
                                                              self.question_word2vec).cuda()
        assert args.answer_embedding in ['MLP']
        #answer_model = getattr(answer_net, args.answer_embedding)(args, self.train_loader.dataset).cuda()
        return fusion_model

    def rel_model(self, args):
        # models api
        fusion_model = getattr(fusion_net, 'SAN_REL')(args, self.train_loader.dataset,
                                                      self.question_word2vec).cuda()
        assert args.answer_embedding in ['MLP']
        #answer_model = getattr(answer_net, args.answer_embedding)(args, self.train_loader.dataset).cuda()
        return fusion_model

    def _model_choice(self, args):
        assert args.fusion_model in ['SAN', 'MLP', 'BAN', 'UD', 'SAN_REL', 'SAN_TAIL']
        # models api
        self.fusion_model = getattr(fusion_net, args.fusion_model)(args, self.train_loader.dataset,
                                                                   self.question_word2vec).cuda()
        # freeze word embedding
        if args.freeze_w2v and args.fusion_model != 'MLP':
            freeze_layer(self.fusion_model.w_emb)

        # answer models
        assert args.method_choice in ['CLS', 'W2V', 'KG', 'GAE', 'KG_W2V', 'KG_GAE', 'GAE_W2V', 'KG_GAE_W2V']
        ans_len_table = {'W2V': 300, 'KG': 300, 'GAE': 1024, 'CLS': 0}
        self.method_list = args.method_choice.split('_')
        self.method_list.sort()
        for i in self.method_list:
            args.ans_feature_len += ans_len_table[i]
        # Mapping-based methods
        if args.method_choice != 'CLS':
            assert args.answer_embedding in ['MLP']
            self.answer_net = getattr(answer_net, args.answer_embedding)(args, self.train_loader.dataset).cuda()
        else:
            # Classifier-based methods
            self.answer_net = SimpleClassifier(args.embedding_size, 2 * args.hidden_size, args.FVQA.max_ans, 0.5).cuda()

    def _updata_best_result(self, max_acc, metrics):
        max_acc[3] = metrics.acc_all
        max_acc[2] = metrics.acc_10
        max_acc[1] = metrics.acc_3
        max_acc[0] = metrics.acc_1

    def _load_model(self, model, function):
        assert function == "fusion" or function == "embedding"
        # support entity mapping
        if self.args.fact_map:
            target = "fact"
        # relation mapping
        elif self.args.relation_map:
            target = "relation"
        else:
            target = "answer"
        model_name = type(model).__name__
        if not self.args.ZSL:
            target = "general_" + target
        save_path = os.path.join(self.args.FVQA.model_save_path, function)
        save_path = os.path.join(save_path, f'{target}_{model_name}_{self.args.FVQA.data_choice}.pkl')

        model.load_state_dict(torch.load(save_path))
        print(f"loading {function} model done!")

    def _load_saved_model(self, model, function, type_name):
        assert function in ["fusion", "embedding"]
        assert type_name in ["answer", "relation", "fact"]
        target = type_name
        model_name = type(model).__name__
        if not self.args.ZSL:
            target = "general_" + target
        save_path = os.path.join(self.args.FVQA.model_save_path, function)
        save_path = os.path.join(save_path, f'{target}_{model_name}_{self.args.FVQA.data_choice}.pkl')

        model.load_state_dict(torch.load(save_path))

        print(f"loading {save_path} model done!")

    def _save_model(self, model, function):
        assert function == "fusion" or function == "embedding"
        if self.args.fact_map:
            target = "fact"
        elif self.args.relation_map:
            target = "relation"
        else:
            target = "answer"
        model_name = type(model).__name__
        if not self.args.ZSL:
            target = "general_" + target
        save_path = os.path.join(self.args.FVQA.model_save_path, function)
        os.makedirs(save_path, exist_ok=True)
        save_path = os.path.join(save_path, f'{target}_{model_name}_{self.args.FVQA.data_choice}.pkl')

        torch.save(model.state_dict(), save_path)
        return save_path


if __name__ == '__main__':
    # Config loading...
    cfg = cfg()
    args = cfg.get_args()
    cfg.update_train_configs(args)
    set_seed(cfg.random_seed)

    # Environment initialization...
    logger = initialize_exp(cfg)
    logger_path = get_dump_path(cfg)
    if not cfg.no_tensorboard:
        writer = SummaryWriter(log_dir=os.path.join(logger_path, 'tensorboard'))

    # torch.cuda.set_device(cfg.gpu_id)

    # Run...
    runner = Runner(cfg)
    runner.run()

    #  information output:
    logger.info(f"best performance = {runner.max_acc[0]: .2f},{runner.max_acc[1]: .2f},{runner.max_acc[2]: .2f}. best epoch = {runner.best_epoch}, correspond_loss={runner.correspond_loss: .4f}")
    if args.ZSL == 1 and not args.fact_map and not args.relation_map:
        logger.info(f" zsl performance = {runner.max_zsl_acc[0]: .2f},{runner.max_zsl_acc[1]: .2f},{runner.max_zsl_acc[2]: .2f}")
    if not cfg.now_test:
        logger.info(f" fusion_model_path = {runner.fusion_model_path}")
        logger.info(f" answer_net_path = {runner.answer_net_path}")
    if not cfg.no_tensorboard:
        writer.close()
