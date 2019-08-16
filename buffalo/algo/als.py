# -*- coding: utf-8 -*-
import time
import json

import numpy as np
from hyperopt import STATUS_OK as HOPT_STATUS_OK

import buffalo.data
from buffalo.misc import aux, log
from buffalo.data.base import Data
from buffalo.algo._als import PyALS
from buffalo.evaluate import Evaluable
from buffalo.algo.options import AlsOption
from buffalo.algo.optimize import Optimizable
from buffalo.data.buffered_data import BufferedDataMatrix
from buffalo.algo.base import Algo, Serializable, TensorboardExtention


class ALS(Algo, AlsOption, Evaluable, Serializable, Optimizable, TensorboardExtention):
    """Python implementation for C-ALS.

    Implementation of Collaborative Filtering for Implicit Feedback datasets.

    Reference: http://yifanhu.net/PUB/cf.pdf"""
    def __init__(self, opt_path, *args, **kwargs):
        Algo.__init__(self, *args, **kwargs)
        AlsOption.__init__(self, *args, **kwargs)
        Evaluable.__init__(self, *args, **kwargs)
        Serializable.__init__(self, *args, **kwargs)
        Optimizable.__init__(self, *args, **kwargs)

        self.logger = log.get_logger('ALS')
        self.opt, self.opt_path = self.get_option(opt_path)
        self.obj = PyALS()
        assert self.obj.init(bytes(self.opt_path, 'utf-8')),\
            'cannot parse option file: %s' % opt_path
        self.data = None
        data = kwargs.get('data')
        data_opt = self.opt.get('data_opt')
        data_opt = kwargs.get('data_opt', data_opt)
        if data_opt:
            self.data = buffalo.data.load(data_opt)
            self.data.create()
        elif isinstance(data, Data):
            self.data = data
        self.logger.info('ALS(%s)' % json.dumps(self.opt, indent=2))
        if self.data:
            self.logger.info(self.data.show_info())
            assert self.data.data_type in ['matrix']

    @staticmethod
    def new(path, data_fields=[]):
        return ALS.instantiate(AlsOption, path, data_fields)

    def set_data(self, data):
        assert isinstance(data, aux.data.Data), 'Wrong instance: {}'.format(type(data))
        self.data = data

    def normalize(self, group='item'):
        if group == 'item':
            self.Q = self._normalize(self.Q)
            self.opt._nrz_Q = True
        elif group == 'user':
            self.P = self._normalize(self.P)
            self.opt._nrz_P = True

    def initialize(self):
        super().initialize()
        self.init_factors()

    def init_factors(self):
        assert self.data, 'Data is not setted'
        header = self.data.get_header()
        for name, rows in [('P', header['num_users']), ('Q', header['num_items'])]:
            setattr(self, name, None)
            setattr(self, name, np.abs(np.random.normal(scale=1.0/(self.opt.d ** 2),
                                       size=(rows, self.opt.d)).astype("float32")))
        self.obj.initialize_model(self.P, self.Q)

    def _get_topk_recommendation(self, rows, topk):
        p = self.P[rows]
        topks = np.argsort(p.dot(self.Q.T), axis=1)[:, -topk:][:,::-1]
        return zip(rows, topks)

    def _get_most_similar_item(self, col, topk):
        return super()._get_most_similar_item(col, topk, self.Q, self.opt._nrz_Q)

    def get_scores(self, row_col_pairs):
        rets = {(r, c): self.P[r].dot(self.Q[c]) for r, c in row_col_pairs}
        return rets

    def _get_buffer(self):
        buf = BufferedDataMatrix()
        buf.initialize(self.data)
        return buf

    def _iterate(self, buf, group='rowwise'):
        header = self.data.get_header()
        # end = header['num_users'] if group == 'rowwise' else header['num_items']
        int_group = 0 if group == 'rowwise' else 1
        self.obj.precompute(int_group)
        err = 0.0
        update_t, feed_t, updated = 0, 0, 0
        buf.set_group(group)
        with log.pbar(log.DEBUG, desc='%s' % group,
                      total=header['num_nnz'], mininterval=30) as pbar:
            start_t = time.time()
            for sz in buf.fetch_batch():
                updated += sz
                feed_t += time.time() - start_t
                start_x, next_x, indptr, keys, vals = buf.get()
                start_t = time.time()
                err += self.obj.partial_update(start_x, next_x, indptr, keys, vals, int_group)
                update_t += time.time() - start_t
                pbar.update(sz)
        self.logger.debug(f'{group} updated: processed({updated}) elapsed(data feed: {feed_t:0.3f}s update: {update_t:0.03}s)')
        return err

    def train(self):
        buf = self._get_buffer()
        best_loss, rmse, self.validation_result = 987654321.0, None, {}
        self.prepare_evaluation()
        self.initialize_tensorboard(self.opt.num_iters)
        for i in range(self.opt.num_iters):
            start_t = time.time()
            self._iterate(buf, group='rowwise')
            err = self._iterate(buf, group='colwise')
            train_t = time.time() - start_t
            rmse = (err / self.data.get_header()['num_nnz']) ** 0.5
            metrics = {'train_loss': rmse}
            if self.opt.validation and self.opt.evaluation_on_learning and self.periodical(self.opt.evaluation_period, i):
                start_t = time.time()
                self.validation_result = self.get_validation_results()
                vali_t = time.time() - start_t
                val_str = ' '.join([f'{k}:{v:0.5f}' for k, v in self.validation_result.items()])
                self.logger.info(f'Validation: {val_str} Elapsed {vali_t:0.3f} secs')
                metrics.update({'val_%s' % k: v
                                for k, v in self.validation_result.items()})
            self.logger.info('Iteration %d: RMSE %.3f Elapsed %.3f secs' % (i + 1, rmse, train_t))
            self.update_tensorboard_data(metrics)
            best_loss = self.save_best_only(rmse, best_loss, i)
            if self.early_stopping(rmse):
                break
        ret = {'train_loss': rmse}
        ret.update({'val_%s' % k: v
                    for k, v in self.validation_result.items()})
        self.finalize_tensorboard()
        return ret

    def _optimize(self, params):
        self._optimize_params = params
        for name, value in params.items():
            assert name in self.opt, 'Unexepcted parameter: {}'.format(name)
            setattr(self.opt, name, value)
        with open(self._temporary_opt_file, 'w') as fout:
            json.dump(self.opt, fout, indent=2)
        assert self.obj.init(bytes(self._temporary_opt_file, 'utf-8')),\
            'cannot parse option file: %s' % self._temporary_opt_file
        self.logger.info(params)
        self.initialize()
        loss = self.train()
        loss['eval_time'] = time.time()
        loss['loss'] = loss.get(self.opt.optimize.loss)
        # TODO: deal with failture of training
        loss['status'] = HOPT_STATUS_OK
        self._optimize_loss = loss
        return loss

    def _get_feature(self, index, group='item'):
        if group == 'item':
            return self.Q[index]
        elif group == 'user':
            return self.P[index]
        return None

    def _get_data(self):
        data = super()._get_data()
        data.extend([('opt', self.opt),
                     ('Q', self.Q),
                     ('P', self.P)])
        return data

    def get_evaluation_metrics(self):
        return ['train_loss', 'val_rmse', 'val_ndcg', 'val_map', 'val_accuracy', 'val_error']