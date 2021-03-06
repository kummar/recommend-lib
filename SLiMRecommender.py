'''
@Author: Yu Di
@Date: 2019-10-27 19:13:22
@LastEditors: Yudi
@LastEditTime: 2019-11-28 16:14:04
@Company: Cardinal Operation
@Email: yudi@shanshu.ai
@Description: SLIM recommender
'''
import os
import gc
import time
import random
import argparse
import operator

import numpy as np
import pandas as pd
from tqdm import tqdm
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

from util import slim
from util.data_loader import SlimData
from util.metrics import map_at_k, ndcg_at_k, hr_at_k, precision_at_k, recall_at_k, mrr_at_k

class SLIM(object):
    def __init__(self, data, i):
        self.data = data
        self.i = i
        print('Start SLIM recommendation......')
        self.A = self.__user_item_matrix()
        self.alpha = None
        self.lam_bda = None
        self.max_iter = None
        self.tol = None # learning threshold
        self.N = None # top-N num
        self.lambda_is_ratio = None        
        self.W = None
        self.recommendation = None
    
    def __user_item_matrix(self):
        A = np.zeros((self.data.num_user, self.data.num_item))
        for user, item in self.data.train[self.i]:
            A[user, item] = 1
        return A

    def __aggregation_coefficients(self):
        group_size = 100  # 并行计算每组计算的行/列数
        n = self.data.num_item // group_size  # 并行计算分组个数
        starts = []
        ends = []
        for i in range(n):
            start = i * group_size
            starts.append(start)
            ends.append(start + group_size)
        if self.data.num_item % group_size != 0:
            starts.append(n * group_size)
            ends.append(self.data.num_item)
            n += 1

        print('covariance updates pre-calculating')
        covariance_array = None
        with ProcessPoolExecutor() as executor:
            covariance_array = np.vstack(list(executor.map(slim.compute_covariance, [self.A] * n, starts, ends)))

        slim.symmetrize_covariance(covariance_array)

        print('coordinate descent for learning W matrix......')
        if self.lambda_is_ratio:
            with ProcessPoolExecutor() as executor:
                return np.hstack(list(executor.map(slim.coordinate_descent_lambda_ratio, 
                                                   [self.alpha] * n, 
                                                   [self.lam_bda] * n, 
                                                   [self.max_iter] * n, 
                                                   [self.tol] * n, 
                                                   [self.data.num_user] * n, 
                                                   [self.data.num_item] * n, 
                                                   [covariance_array] * n, 
                                                   starts, ends)))
        else:
            with ProcessPoolExecutor() as executor:
                return np.hstack(list(executor.map(slim.coordinate_descent, 
                                                   [self.alpha] * n, 
                                                   [self.lam_bda] * n, 
                                                   [self.max_iter] * n, 
                                                   [self.tol] * n, 
                                                   [self.data.num_user] * n, 
                                                   [self.data.num_item] * n, 
                                                   [covariance_array] * n, 
                                                   starts, ends)))
    
    def __recommend(self, u, user_AW, user_item_set, method='test'):
        '''
        generate N recommend items for user
        :param user_AW: the user row of the result of matrix dot product of A and W
        :param user_item_set: item interacted in train set for user 
        :return: recommend list for user
        '''
        if method == 'test':
            truth = self.ur[u]
        elif method == 'val':
            truth = self.val_ur[u]
        max_i_num = 1000
        if len(truth) < max_i_num:
            cands_num = max_i_num - len(truth)
            sub_item_pool = set(range(self.data.num_item)) - user_item_set - set(truth)
            cands = random.sample(sub_item_pool, cands_num)
            candidates = list(set(truth) | set(cands))
        else:
            candidates = random.sample(truth, max_i_num)

        rank = dict()
        for i in set(candidates):
            rank[i] = user_AW[i]
        return [items[0] for items in sorted(rank.items(), key=operator.itemgetter(1), reverse=True)[:self.N]]

    def __get_recommendation(self, method='test'):
        train_user_items = [set() for u in range(self.data.num_user)]
        for user, item in self.data.train[self.i]:
            train_user_items[user].add(item)

        AW = self.A.dot(self.W) # get user prediction for all item
        # recommend N items for each user
        recommendation = []
        for u, user_AW, user_item_set in zip(range(self.data.num_user), AW, train_user_items):
            recommendation.append(self.__recommend(u, user_AW, user_item_set, method))
        return recommendation

    def compute_recommendation(self, alpha=0.5, lam_bda=0.02, max_iter=1000, tol=0.0001, N=10, 
                               ground_truth=None, val_ur=None, lambda_is_ratio=True):
        self.alpha = alpha
        self.lam_bda = lam_bda
        self.max_iter = max_iter
        self.tol = tol
        self.N = N
        self.lambda_is_ratio = lambda_is_ratio
        self.ur = ground_truth

        self.val_ur = val_ur

        print(f'Start calculating W matrix(alpha={self.alpha}, lambda={self.lam_bda}, max_iter={self.max_iter}, tol={self.tol})')
        self.W = self.__aggregation_coefficients()

        print(f'Start calculating validation recommendation list(N={self.N})')
        self.val_recommendation = self.__get_recommendation('val')

        print(f'Start calculating recommendation list(N={self.N})')
        self.recommendation = self.__get_recommendation('test')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepro', 
                        type=str, 
                        default='origin', 
                        help='dataset type for experiment, origin, 5core, 10core available')
    parser.add_argument('--topk', 
                        type=int, 
                        default=10, 
                        help='top number of recommend list')
    parser.add_argument('--alpha', 
                        type=float, 
                        default=0.5, 
                        help='ratio if lasso result, 0 for ridge-regression, 1 for lasso-regression')
    parser.add_argument('--elastic', 
                        type=float, 
                        default=0.02, 
                        help='elastic net parameter')
    parser.add_argument('--epochs', 
                        type=int, 
                        default=1000, 
                        help='No. of learning iteration')
    parser.add_argument('--tol', 
                        type=float, 
                        default=0.0001, 
                        help='learning threshold')
    parser.add_argument('--data_split', 
                        type=str, 
                        default='fo', 
                        help='method for split test,options: loo/fo')
    parser.add_argument('--by_time', 
                        type=int, 
                        default=0, 
                        help='whether split data by time stamp')
    parser.add_argument('--dataset', 
                        type=str, 
                        default='ml-100k', 
                        help='select dataset')
    parser.add_argument('--val_method', 
                        type=str, 
                        default='cv', 
                        help='validation method, options: cv, tfo, loo, tloo')
    parser.add_argument('--fold_num', 
                        type=int, 
                        default=5, 
                        help='No. of folds for cross-validation')
    args = parser.parse_args()

    slim_data= SlimData(args.dataset, args.data_split, args.by_time, args.val_method, args.fold_num, args.prepro)

    # genereate top-N list for test user set
    test_user_set = list({ele[0] for ele in slim_data.test})
    # this ur is test set ground truth
    test_ur = defaultdict(list)
    for ele in slim_data.test:
        test_ur[ele[0]].append(ele[1])
    # this val_ur_list is validation set ground_truth
    val_ur_list = []
    for validation in slim_data.val:
        tmp_ur = defaultdict(list)
        for ele in validation:
            tmp_ur[ele[0]].append(ele[1])
        val_ur_list.append(tmp_ur)

    recommender_list = []
    val_kpi = []
    fnl_precision, fnl_recall, fnl_map, fnl_ndcg, fnl_hr, fnl_mrr = [], [], [], [], [], []
    start_time = time.time()
    for i in range(len(slim_data.train)):
        val_ur = val_ur_list[i]
        recommend = SLIM(slim_data, i)
        recommend.compute_recommendation(alpha=args.alpha, lam_bda=args.elastic, max_iter=args.epochs, 
                                         tol=args.tol, N=args.topk, ground_truth=ur, val_ur=val_ur)
        print('Finish train model and generate topN list')
        recommender_list.append(recommend)

        # validation predictions
        preds = {}
        for u in val_ur.keys():
            preds[u] = recommend.val_recommendation[u]
        for u in preds.keys():
            preds[u] = [1 if e in val_ur[u] else 0 for e in preds[u]]
        
        val_kpi_k = np.mean([precision_at_k(r, args.topk) for r in preds.values()])
        val_kpi.append(val_kpi_k)
        
        # test predictions
        preds = {}
        for u in test_ur.keys():
            preds[u] = recommend.recommendation[u] # recommendation didn't contain item in train set
        for u in preds.keys():
            preds[u] = [1 if e in test_ur[u] else 0 for e in preds[u]]
        # calculate metrics
        precision_k = np.mean([precision_at_k(r, args.topk) for r in preds.values()])
        fnl_precision.append(precision_k)

        recall_k = np.mean([recall_at_k(r, len(test_ur[u]), args.topk) for u, r in preds.items()])
        fnl_recall.append(recall_k)

        map_k = map_at_k(list(preds.values()))
        fnl_map.append(map_k)

        ndcg_k = np.mean([ndcg_at_k(r, args.topk) for r in preds.values()])
        fnl_ndcg.append(ndcg_k)

        hr_k = hr_at_k(list(preds.values()), list(preds.keys()), test_ur)
        fnl_hr.append(hr_k)

        mrr_k = mrr_at_k(list(preds.values()))
        fnl_mrr.append(mrr_k)
        
    for i in range(len(val_kpi)):
        print(f'Validation [{i + 1}] Precision@{args.topk}: {val_kpi[i]}')

    print('---------------------------------')
    print(f'Precision@{args.topk}: {np.mean(fnl_precision)}')
    print(f'Recall@{args.topk}: {np.mean(fnl_recall)}')
    print(f'MAP@{args.topk}: {np.mean(fnl_map)}')
    print(f'NDCG@{args.topk}: {np.mean(fnl_ndcg)}')
    print(f'HR@{args.topk}: {np.mean(fnl_hr)}')
    print(f'MRR@{args.topk}: {np.mean(fnl_mrr)}')
