# -*- coding: utf-8 -*-
import os
import abc
import json
import time
from buffalo.misc import aux, log
from buffalo.misc.log import pbar

from hyperopt import hp, fmin, tpe, Trials


class Optimizable(object):
    def __init__(self, *args, **kwargs):
        self._optimization_info = {'trials': Trials(), 'best': {}}
        self._temporary_opt_file = aux.get_temporary_file()
        self.optimize_after_callback_fn = kwargs.get('optimize_after_callback_fn')

    def __del__(self):
        os.remove(self._temporary_opt_file)

    def _get_space(self, options):
        spaces = {}
        for param_name, data in options.items():
            algo, opt = data
            if algo == 'randint':
                spaces[param_name] = opt[1] + hp.randint(opt[0], opt[2] - opt[1])
            else:
                spaces[param_name] = getattr(hp, algo)(*opt)
        return spaces

    @abc.abstractmethod
    def _optimize(self):
        raise NotImplemented

    def optimize(self):
        opt = self.opt.optimize
        iters, max_trials = 0, opt.get('max_trials', -1)
        space = self._get_space(opt.space)
        with log.pbar(log.INFO, desc='optimizing... ',
                      total=None if max_trials == -1 else max_trials,
                      mininterval=30) as pbar:
            if opt.start_with_default_parameters:
                with log.supress_log_level(log.WARN):
                    loss = self._optimize({})
                self.logger.info(f'Starting with default parameter result: {loss}')
                self._optimization_info['best'] = loss
            while(max_trials):
                with log.supress_log_level(log.WARN):
                    best = fmin(fn=self._optimize,
                                space=space,
                                algo=tpe.suggest,
                                max_evals=len(self._optimization_info['trials'].trials) + 1,
                                trials=self._optimization_info['trials'],
                                show_progressbar=False)
                iters += 1
                max_trials -= 1
                if self._optimization_info.get('best', {}).get('loss', 987654321) > self._optimize_loss['loss']:
                    is_first_time = self._optimization_info['best'] == {}
                    best = self._optimize_loss  # we cannot use return value of hyperopt due to randint behavior patch
                    self.logger.info(f'Found new best parameters: {best} @ iter {iters}')
                    self._optimization_info['best'] = best
                    if opt.deployment and (is_first_time or not opt.min_trials or opt.min_trials >= iters):
                        if not self.opt.model_path:
                            raise RuntimeError('Failed to dump model: model path is not defined')
                        self.logger.info('Saving model... to {}'.format(self.opt.model_path))
                        self.dump(self.opt.model_path)
                if self.optimize_after_callback_fn:
                    self.optimize_after_callback_fn(self._optimization_info)
                pbar.update(1)
                self.logger.debug('Params({}) Losses({})'.format(self._optimize_params, self._optimize_loss))

    def get_optimization_data(self):
        return self._optimization_info
