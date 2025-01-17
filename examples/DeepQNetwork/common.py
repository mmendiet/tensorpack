# -*- coding: utf-8 -*-
# File: common.py
# Author: Yuxin Wu

import multiprocessing
import numpy as np
import random
import time
from six.moves import queue

from tensorpack.callbacks import Callback
from tensorpack.utils import logger, get_tqdm
from tensorpack.utils.concurrency import ShareSessionThread, StoppableThread
from tensorpack.utils.stats import StatCounter


def play_one_episode(env, func, render=False):
    def predict(s):
        """
        Map from observation to action, with 0.01 greedy.
        """
        s = np.expand_dims(s, 0)  # batch
        act = func(s)[0][0].argmax()
        if random.random() < 0.01:
            spc = env.action_space
            act = spc.sample()
        return act

    ob = env.reset()
    sum_r = 0
    while True:
        act = predict(ob)
        ob, r, isOver, info = env.step(act)
        if render:
            env.render()
        sum_r += r
        if isOver:
            return sum_r

def play_my_episode(env, func, filePath, traj, render=False):
    def predict(s):
        """
        Map from observation to action, with 0.01 greedy.
        """
        s = np.expand_dims(s, 0)  # batch
        act = func(s)[0][0].argmax()
        if random.random() < 0.01:
            spc = env.action_space
            act = spc.sample()
        return act
    import torch
    from collections import defaultdict
    import pickle
    partition = defaultdict(list)
    labels = defaultdict(list)
    train_list = []
    validation_list = []
    fileName = filePath.split('/')[1]
    saveDir = "/1TB/Datasets/Atari/data3/data_traj/"
    dictDir = "/1TB/Datasets/Atari/data3/data_dict/"
    steps = 0
    while steps < traj:#1728000:
        ob = env.reset()
        ep_step = 1
        print(steps)
        while (ep_step <= 4000) and (steps < traj):
            act = predict(ob)

            action_image = np.full((84,84,1), float(act))
            curr_state_rsa = np.concatenate((ob[:,:,:,0],ob[:,:,:,1],ob[:,:,:,2],ob[:,:,:,3], action_image), axis=2)

            ob, r, isOver, info = env.step(act)

            reward_image = np.full((84,84,1), float(r))
            next_frame_r = np.concatenate((ob[:,:,:,3], reward_image), axis=2)

            input_tensor = torch.from_numpy(curr_state_rsa).float()
            output_tensor = torch.from_numpy(next_frame_r).float()

            if(ep_step%4==0):
                ID = fileName.split('.')[0] + '_' + str(steps)
                np.savez_compressed(saveDir+str(ID), input_tensor)
                outkey = str(ID)+"_out"
                np.savez_compressed(saveDir+outkey, output_tensor)
                #torch.save(input_tensor, saveDir+str(ID)+".pt")
                if ((steps-8)%10==0) or ((steps-9)%10==0):
                    validation_list.append(ID)
                else: #80000000  8000000
                    train_list.append(ID)
                labels[ID] = outkey

                steps += 1
            if isOver:
                print(steps)
                break
            ep_step += 1
        partition['train'] = train_list
        partition['validation'] = validation_list
        fp = open(dictDir+"partition_"+fileName,"wb")
        pickle.dump(partition,fp)
        fp.close()

        fl = open(dictDir+"labels_"+fileName,"wb")
        pickle.dump(labels,fl)
        fl.close()


def play_n_episodes(player, predfunc, nr, render=False):
    logger.info("Start Playing ... ")
    for k in range(nr):
        score = play_one_episode(player, predfunc, render=render)
        print("{}/{}, score={}".format(k, nr, score))


def eval_with_funcs(predictors, nr_eval, get_player_fn, verbose=False):
    """
    Args:
        predictors ([PredictorBase])
    """
    class Worker(StoppableThread, ShareSessionThread):
        def __init__(self, func, queue):
            super(Worker, self).__init__()
            self._func = func
            self.q = queue

        def func(self, *args, **kwargs):
            if self.stopped():
                raise RuntimeError("stopped!")
            return self._func(*args, **kwargs)

        def run(self):
            with self.default_sess():
                player = get_player_fn(train=False)
                while not self.stopped():
                    try:
                        score = play_one_episode(player, self.func)
                    except RuntimeError:
                        return
                    self.queue_put_stoppable(self.q, score)

    q = queue.Queue()
    threads = [Worker(f, q) for f in predictors]

    for k in threads:
        k.start()
        time.sleep(0.1)  # avoid simulator bugs
    stat = StatCounter()

    def fetch():
        r = q.get()
        stat.feed(r)
        if verbose:
            logger.info("Score: {}".format(r))

    for _ in get_tqdm(range(nr_eval)):
        fetch()
    # waiting is necessary, otherwise the estimated mean score is biased
    logger.info("Waiting for all the workers to finish the last run...")
    for k in threads:
        k.stop()
    for k in threads:
        k.join()
    while q.qsize():
        fetch()

    if stat.count > 0:
        return (stat.average, stat.max)
    return (0, 0)


def eval_model_multithread(pred, nr_eval, get_player_fn):
    """
    Args:
        pred (OfflinePredictor): state -> [#action]
    """
    NR_PROC = min(multiprocessing.cpu_count() // 2, 8)
    with pred.sess.as_default():
        mean, max = eval_with_funcs(
            [pred] * NR_PROC, nr_eval,
            get_player_fn, verbose=True)
    logger.info("Average Score: {}; Max Score: {}".format(mean, max))


class Evaluator(Callback):
    def __init__(self, nr_eval, input_names, output_names, get_player_fn):
        self.eval_episode = nr_eval
        self.input_names = input_names
        self.output_names = output_names
        self.get_player_fn = get_player_fn

    def _setup_graph(self):
        NR_PROC = min(multiprocessing.cpu_count() // 2, 20)
        self.pred_funcs = [self.trainer.get_predictor(
            self.input_names, self.output_names)] * NR_PROC

    def _trigger(self):
        t = time.time()
        mean, max = eval_with_funcs(
            self.pred_funcs, self.eval_episode, self.get_player_fn)
        t = time.time() - t
        if t > 10 * 60:  # eval takes too long
            self.eval_episode = int(self.eval_episode * 0.94)
        self.trainer.monitors.put_scalar('mean_score', mean)
        self.trainer.monitors.put_scalar('max_score', max)
