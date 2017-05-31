import tensorflow as tf
import numpy as np
import gym
from gym import wrappers
from threading import Thread
import sys
from AtariPreprocessor import AtariPreprocessor

from test_env import EnvTest

class Config:
    num_actions = None
    state_shape = None
    
    lr = 1e-4
    grad_clip = 40.
    rnn_hidden_size = 256
    gamma = .99

class PolicyNetwork():
    def __init__(self, config, scope, parent_network=None, summarizer=None):
        self.config = config
        self.scope = scope
        
        init_params = {
            "kernel_initializer": tf.contrib.layers.xavier_initializer(),
            "bias_initializer": tf.zeros_initializer()
        }
        
        # If we have a parent network, make these local variables
        if parent_network is not None:
            def custom_getter(getter, *args, **kwargs):
                if kwargs['collections'] is None:
                    kwargs['collections'] = []
                kwargs['collections'] += [tf.GraphKeys.LOCAL_VARIABLES]
                return getter(*args, **kwargs)
        else:
            custom_getter = None

        if parent_network is None:
            parent_network = self
        
        with tf.variable_scope(scope, custom_getter=custom_getter):
            self.states = states = tf.placeholder(tf.float32, shape=(None,) + self.config.state_shape)
            
            # The main architecture: 4 conv layers into an LSTM, into two fully connected layers.
            out = states
            for i in range(4):
                out = tf.layers.conv2d(
                    inputs=out,
                    filters=32,
                    kernel_size=3,
                    strides=2,
                    padding="same",
                    activation=tf.nn.elu,
                    **init_params)
                
            cell = tf.contrib.rnn.BasicLSTMCell(self.config.rnn_hidden_size)
            rnn_state = cell.zero_state(1, tf.float32)
            
            out = tf.contrib.layers.flatten(inputs=out)
            out, next_rnn_state = tf.nn.dynamic_rnn(cell, tf.expand_dims(out, 0), initial_state=rnn_state)
            out = tf.squeeze(out, [0])

            policy_logits = tf.layers.dense(
                inputs=out,
                units=self.config.num_actions,
                **init_params
            )
            log_policies = tf.nn.log_softmax(policy_logits)
            policies = tf.nn.softmax(policy_logits)
            values = tf.layers.dense(
                inputs=out,
                units=1,
                **init_params
            )
            values = tf.squeeze(values, -1)
            
            self.rnn_state = rnn_state
            self.next_rnn_state = next_rnn_state
            self.log_policies = log_policies
            self.policies = policies
            self.values = values
            
            # Synchronizing variables.
            all_variables = sorted(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope), key=lambda v: v.name)
            self.all_variables = all_variables
            
            sync_op = tf.group(*[tf.assign(t, s) for s, t in zip(parent_network.all_variables, all_variables)])
            self.sync_op = sync_op

            # Gradients.
            self.actions = actions = tf.placeholder(tf.uint8, shape=(None,))
            self.empirical_values = empirical_values = tf.placeholder(tf.float32, shape=(None,))
            
            policy_loss = -tf.einsum('ij,ij,i->',
                log_policies,
                tf.one_hot(actions, self.config.num_actions),
                empirical_values - tf.stop_gradient(values))
            entropy_loss = tf.einsum('ij,ij->', policies, log_policies)
            value_loss = tf.nn.l2_loss(empirical_values - values)
            loss = policy_loss + .01*entropy_loss + .25*value_loss
            
            optimizer = tf.train.AdamOptimizer(self.config.lr)
            self.optimizer = optimizer
            
            grads = tf.gradients(loss, all_variables)
            grads, grad_norm = tf.clip_by_global_norm(grads, self.config.grad_clip)
            train_op = parent_network.optimizer.apply_gradients(zip(grads, parent_network.all_variables))
            
            self.grad_norm = grad_norm
            self.train_op = train_op
            
            # Summaries
            if summarizer is not None:
                tf.summary.scalar('gradient norm', grad_norm)
                self.summaries = tf.summary.merge_all()
        
    def synchronize(self, sess):
        return sess.run(self.sync_op)
    
    def get_next_policy(self, sess, state, rnn_state=None):
        if rnn_state is None:
            policies, values, next_rnn_state = sess.run([self.policies, self.values, self.next_rnn_state], {
                self.states: [state]
            })
        else:
            policies, values, next_rnn_state = sess.run([self.policies, self.values, self.next_rnn_state], {
                self.states: [state],
                self.rnn_state[0]: rnn_state[0],
                self.rnn_state[1]: rnn_state[1]
            })
            
        return policies[0], values[0], next_rnn_state
    
    def apply_gradients_from_rollout(self, sess, states, actions, empirical_values, rnn_initial_state=None):
        if rnn_initial_state is None:
            gn, _ = sess.run([self.grad_norm, self.train_op], feed_dict={
                self.states: states,
                self.actions: actions,
                self.empirical_values: empirical_values
            })
        else:
            gn, _ = sess.run([self.grad_norm, self.train_op], feed_dict={
                self.states: states,
                self.actions: actions,
                self.empirical_values: empirical_values,
                self.rnn_state[0]: rnn_initial_state[0],
                self.rnn_state[1]: rnn_initial_state[1]
            })
            
        return gn

def worker(coord, sess, policy_network, env):
    with coord.stop_on_exception():
        done = True
        state = None
        rnn_state = None
        episode_reward = 0
        
        while True:
            policy_network.synchronize(sess)
            if done:
                done = False
                state = env.reset()
                rnn_state = None
                episode_reward = 0

            # Take a few steps
            history = []
            estimated_value = 0
            rnn_initial_state = rnn_state
            for _ in xrange(20):
                policy, estimated_value, rnn_state = policy_network.get_next_policy(sess, state, rnn_state)
                action = np.random.choice(policy_network.config.num_actions, p=policy)
                new_state, reward, done, _ = env.step(action)
                episode_reward += reward

                history.append((state, action, reward))
                state = new_state
                if done:
                    print ""
                    print policy_network.scope, 'episode reward: ', episode_reward
                    break
                
                if coord.should_stop():
                    return

            # Process the experience
            states, actions, rewards = map(np.array, zip(*history))
            print policy_network.scope, 'reward: ', np.sum(rewards)

            future_rewards = [0 if done else estimated_value]
            for reward in reversed(rewards.tolist()[:-1]):
                future_rewards.append(reward + policy_network.config.gamma * future_rewards[-1])
            empirical_values = np.array(list(reversed(future_rewards)))

            policy_network.apply_gradients_from_rollout(sess, states, actions, empirical_values, rnn_initial_state)


def main():
    make_env = lambda: EnvTest((5, 5, 1))
    #make_env = lambda: AtariPreprocessor(gym.make('Pong-v0'))
    log_dir = 'envtest_training'
    num_workers = 3
    
    #env = wrappers.Monitor(env, 'results/1')
    
    # Define config
    env = make_env()
    config = Config()
    config.num_actions = env.action_space.n
    config.state_shape = env.observation_space.shape
    
    # Construct the graph
    global_network = PolicyNetwork(config, 'global')
    local_networks = [PolicyNetwork(config, 'worker' + str(i), parent_network=global_network) for i in range(num_workers)]
    
    print 'global: ', [v.name for v in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)]
    print ''
    print 'local: ', [v.name for v in tf.get_collection(tf.GraphKeys.LOCAL_VARIABLES)]
    
    saver = tf.train.Saver()
    sv = tf.train.Supervisor(logdir=log_dir, saver=saver, save_model_secs=10)
    with sv.managed_session() as sess:
        #writer = tf.summary.FileWriter('log', sess.graph)

        # Create threads
        #coord = tf.train.Coordinator()
        coord = sv.coord
        threads = [Thread(target=worker, args=(coord, sess, network, make_env())) for network in local_networks]

        print "Starting training..."
        try:
            for t in threads:
                t.start()
            coord.join(threads, stop_grace_period_secs=5)
        except KeyboardInterrupt:
            coord.request_stop()
            raise

main()
