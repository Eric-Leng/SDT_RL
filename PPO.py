"""
Dependencies:
python:3.5
tensorflow r1.3
"""

import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt
import traceback
import threading
import queue
from environment import Adjust_env
import math
from flask import Flask
from flask import jsonify
from flask import request

app = Flask(__name__)

EP_MAX = 20000
EP_LEN = 3000
N_WORKER = 4  # parallel workers
GAMMA = 0.9  # reward discount factor
A_LR = 0.0001  # learning rate for actor
C_LR = 0.0002  # learning rate for critic
MIN_BATCH_SIZE = 64  # minimum batch size for updating PPO
UPDATE_STEP = 10  # loop update operation n-steps
EPSILON = 0.2  # for clipping surrogate objective
S_DIM, A_DIM = 3, 1  # state and action dimension
LAST_POINT_ADJUST_TIME = 1000 * 60 * 30
environments = {}


@app.route('/adjust', methods=['POST'])
def adjust():
    if request.method == 'POST':
        try:
            last_compress_point = request.json
            # print(last_compress_point)
            key = last_compress_point['lastPoint']['assertId'] + '/' + last_compress_point['lastPoint'][
                'modelId'] + '/' + last_compress_point['lastPoint']['measurement']
            print(key)
            last_compress_point = adjust_param(last_compress_point, key)
            return jsonify({'status': 0, "msg": 'OK', "data": last_compress_point})
        except Exception as e:
            return jsonify({'status': 1, "msg": 'rl adjust had something wrong!'})
    else:
        return jsonify({'status': 1, "msg": 'rl adjust api should be post'})


@app.route('/adjust', methods=['GET'])
def adjust_get():
    return jsonify({'status': 1, "msg": 'start rl adjust wrong'})


class PPO(object):
    def __init__(self):
        self.sess = tf.compat.v1.Session()
        self.tfs = tf.compat.v1.placeholder(tf.float32, [None, S_DIM], 'state')

        # critic
        l1 = tf.layers.dense(self.tfs, 100, tf.nn.relu)
        self.v = tf.layers.dense(l1, 1)
        self.tfdc_r = tf.placeholder(tf.float32, [None, 1], 'discounted_r')
        self.advantage = self.tfdc_r - self.v
        self.closs = tf.reduce_mean(tf.square(self.advantage))
        self.ctrain_op = tf.compat.v1.train.AdamOptimizer(C_LR).minimize(self.closs)

        # actor
        pi, pi_params = self._build_anet('pi', trainable=True)
        oldpi, oldpi_params = self._build_anet('oldpi', trainable=False)
        self.sample_op = tf.squeeze(pi.sample(1), axis=0)  # operation of choosing action
        self.update_oldpi_op = [oldp.assign(p) for p, oldp in zip(pi_params, oldpi_params)]

        self.tfa = tf.compat.v1.placeholder(tf.float32, [None, A_DIM], 'action')
        self.tfadv = tf.compat.v1.placeholder(tf.float32, [None, 1], 'advantage')
        # ratio = tf.exp(pi.log_prob(self.tfa) - oldpi.log_prob(self.tfa))
        ratio = pi.prob(self.tfa) / (oldpi.prob(self.tfa) + 1e-5)
        surr = ratio * self.tfadv  # surrogate loss

        self.aloss = -tf.reduce_mean(tf.minimum(  # clipped surrogate objective
            surr,
            tf.clip_by_value(ratio, 1. - EPSILON, 1. + EPSILON) * self.tfadv))

        self.atrain_op = tf.train.AdamOptimizer(A_LR).minimize(self.aloss)
        self.sess.run(tf.compat.v1.global_variables_initializer())

    def update(self):
        global GLOBAL_UPDATE_COUNTER
        while not COORD.should_stop():
            UPDATE_EVENT.wait()  # wait until get batch of data
            self.sess.run(self.update_oldpi_op)  # copy pi to old pi
            data = [QUEUE.get() for _ in range(QUEUE.qsize())]  # collect data from all workers
            data = np.vstack(data)
            s, a, r = data[:, :S_DIM], data[:, S_DIM: S_DIM + A_DIM], data[:, -1:]
            adv = self.sess.run(self.advantage, {self.tfs: s, self.tfdc_r: r})
            # update actor and critic in a update loop
            [self.sess.run(self.atrain_op, {self.tfs: s, self.tfa: a, self.tfadv: adv}) for _ in range(UPDATE_STEP)]
            [self.sess.run(self.ctrain_op, {self.tfs: s, self.tfdc_r: r}) for _ in range(UPDATE_STEP)]
            UPDATE_EVENT.clear()  # updating finished
            GLOBAL_UPDATE_COUNTER = 0  # reset counter
            ROLLING_EVENT.set()  # set roll-out available

    def _build_anet(self, name, trainable):
        with tf.variable_scope(name):
            l1 = tf.layers.dense(self.tfs, 200, tf.nn.relu, trainable=trainable)
            mu = 2 * tf.layers.dense(l1, A_DIM, tf.nn.tanh, trainable=trainable)
            sigma = tf.layers.dense(l1, A_DIM, tf.nn.softplus, trainable=trainable)
            norm_dist = tf.distributions.Normal(loc=mu, scale=sigma)
        params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=name)
        return norm_dist, params

    def choose_action(self, s):
        step_len = s[0]
        s = s[np.newaxis, :]
        a = self.sess.run(self.sample_op, {self.tfs: s})[0]
        return np.clip(a, 0.5 * step_len, 1.5 * step_len)

    def get_v(self, s):
        if s.ndim < 2: s = s[np.newaxis, :]
        return self.sess.run(self.v, {self.tfs: s})[0, 0]


class Worker(object):
    def __init__(self, wid):
        self.wid = wid
        self.environment = Adjust_env()
        self.ppo = GLOBAL_PPO

    def work(self):
        global GLOBAL_EP, GLOBAL_RUNNING_R, GLOBAL_UPDATE_COUNTER
        while not COORD.should_stop():
            s = self.environment.reset()
            ep_r = 0
            buffer_s, buffer_a, buffer_r = [], [], []
            for t in range(EP_LEN):
                if not ROLLING_EVENT.is_set():  # while global PPO is updating
                    ROLLING_EVENT.wait()  # wait until PPO is updated
                    buffer_s, buffer_a, buffer_r = [], [], []  # clear history buffer
                a = self.ppo.choose_action(s)
                s_, r = self.environment.step(a)
                buffer_s.append(s)
                buffer_a.append(a)
                buffer_r.append(r)  # normalize reward, find to be useful
                s = s_
                ep_r += r

                GLOBAL_UPDATE_COUNTER += 1  # count to minimum batch size
                if t == EP_LEN - 1 or GLOBAL_UPDATE_COUNTER >= MIN_BATCH_SIZE:
                    v_s_ = self.ppo.get_v(s_)
                    discounted_r = []  # compute discounted reward
                    for r in buffer_r[::-1]:
                        v_s_ = r + GAMMA * v_s_
                        discounted_r.append(v_s_)
                    discounted_r.reverse()

                    bs, ba, br = np.vstack(buffer_s), np.vstack(buffer_a), np.array(discounted_r)[:, np.newaxis]
                    buffer_s, buffer_a, buffer_r = [], [], []
                    QUEUE.put(np.hstack((bs, ba, br)))
                    if GLOBAL_UPDATE_COUNTER >= MIN_BATCH_SIZE:
                        ROLLING_EVENT.clear()  # stop collecting data
                        UPDATE_EVENT.set()  # globalPPO update

                    if GLOBAL_EP >= EP_MAX:  # stop training
                        COORD.request_stop()
                        break

            # record reward changes, plot later
            if len(GLOBAL_RUNNING_R) == 0:
                GLOBAL_RUNNING_R.append(ep_r)
            else:
                GLOBAL_RUNNING_R.append(GLOBAL_RUNNING_R[-1] * 0.9 + ep_r * 0.1)
            GLOBAL_EP += 1
            print('{0:.1f}%'.format(GLOBAL_EP / EP_MAX * 100), '|W%i' % self.wid, '|Ep_r: %.2f' % ep_r, )


def adjust_param(last_compress_point, key):
    if key in environments:
        env = environments[key]
    else:
        env = Adjust_env()
        environments[key] = env
    next_time = last_compress_point['nextTime']
    comp_frequency = last_compress_point['compFrequency']
    comp_error = last_compress_point['compError']
    comp_dev_old = last_compress_point['compDev']
    before_comp = last_compress_point['beforeComp']
    after_comp = last_compress_point['afterComp']
    comp_std = math.sqrt(comp_error / before_comp)
    comp_proportion = before_comp / after_comp
    update = {'comp_dev': comp_dev_old, 'comp_proportion': comp_proportion, 'comp_std': comp_std}
    env.update(update)
    s = env.getstate()
    a = GLOBAL_PPO.choose_action(s)
    s, r = env.step(a)
    comp_dev = s[0]

    last_compress_point['compDev'] = comp_dev
    last_compress_point['nextTime'] = next_time + LAST_POINT_ADJUST_TIME
    last_compress_point['beforeSum'] = last_compress_point['beforeSum'] + before_comp
    last_compress_point['afterSum'] = last_compress_point['afterSum'] + after_comp
    last_compress_point['beforeComp'] = 0
    last_compress_point['afterComp'] = 0
    last_compress_point['compStd'] = last_compress_point['compStd'] + comp_std
    last_compress_point['compError'] = 0
    last_compress_point['compFrequency'] = comp_frequency + 1

    print('update', ' compDev from ', comp_dev_old, ' to ', comp_dev)
    return last_compress_point


if __name__ == '__main__':
    GLOBAL_PPO = PPO()
    UPDATE_EVENT, ROLLING_EVENT = threading.Event(), threading.Event()
    UPDATE_EVENT.clear()  # not update now
    ROLLING_EVENT.set()  # start to roll out
    workers = [Worker(wid=i) for i in range(N_WORKER)]

    GLOBAL_UPDATE_COUNTER, GLOBAL_EP = 0, 0
    GLOBAL_RUNNING_R = []
    COORD = tf.train.Coordinator()
    QUEUE = queue.Queue()  # workers putting data in this queue

    # plot reward change and test
    # plt.plot(np.arange(len(GLOBAL_RUNNING_R)), GLOBAL_RUNNING_R)
    # plt.xlabel('Episode')
    # plt.ylabel('Moving reward')
    # plt.ion()
    # plt.show()
    app.run(host='0.0.0.0', port=8080, debug=True)
    adjust_get.run(host='0.0.0.0', port=8080, debug=True)

    # env = Adjust_env()
