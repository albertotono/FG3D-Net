# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Train a Fast R-CNN network."""

from fast_rcnn.config_car import cfg
import gt_data_layer.roidb_car as gdl_roidb
import roi_data_layer.roidb_car as rdl_roidb
from roi_data_layer.layer_carv1_test import RoIDataLayer
from utils.timer import Timer
import numpy as np
import os
import tensorflow as tf
import sys
from tensorflow.python.client import timeline
import time
from tensorflow.core.protobuf import saver_pb2
import math
from tensorflow.contrib import rnn
import random
import networks.bag_of_feature as bg
import scipy.io as sio

class SolverWrapper(object):
    """A simple wrapper around Caffe's solver.
    This wrapper gives us control over he snapshotting process, which we
    use to unnormalize the learned bounding-box regression weights.
    """

    def __init__(self, sess, saver, network,network1,network2,network3,network4,network5,network6,network7,network8,network9,network10,network11, imdb, roidb, output_dir, pretrained_model=None):
        """Initialize the SolverWrapper."""
        #with tf.variable_scope("faster_rcnn", reuse=True) as scope:
        self.net = network
        self.net1 = network1
        self.net2 = network2
        self.net3 = network3
        self.net4 = network4
        self.net5 = network5
        self.net6 = network6
        self.net7 = network7
        self.net8 = network8
        self.net9 = network9
        self.net10 = network10
        self.net11 = network11



        self.imdb = imdb
        self.roidb = roidb
        self.output_dir = output_dir
        self.pretrained_model = pretrained_model
        self.proposal_number = 20
        self.views = 12
        self.classes = 20
        self.cluster_size = 64
        self.output_dim = 512
        self.feature_size = 512
        self.rnn_steps = 12
        self.hidden_size = 4096

        print 'Computing bounding-box regression targets...'
        if cfg.TRAIN.BBOX_REG:
            self.bbox_means, self.bbox_stds = rdl_roidb.add_bbox_regression_targets(roidb)
        print 'done'

        # For checkpoint
        self.saver = saver

    def snapshot(self, sess, iter):
        """Take a snapshot of the network after unnormalizing the learned
        bounding-box regression weights. This enables easy use at test-time.
        """
        with tf.variable_scope('build', reuse=True):
            net = self.net

            if cfg.TRAIN.BBOX_REG and net.layers.has_key('bbox_pred'):
                # save original values
                with tf.variable_scope('bbox_pred', reuse=True):
                    weights = tf.get_variable("weights")
                    biases = tf.get_variable("biases")

                orig_0 = weights.eval()
                orig_1 = biases.eval()

                # scale and shift with bbox reg unnormalization; then save snapshot
                weights_shape = weights.get_shape().as_list()
                sess.run(net.bbox_weights_assign, feed_dict={net.bbox_weights: orig_0 * np.tile(self.bbox_stds, (weights_shape[0], 1))})
                sess.run(net.bbox_bias_assign, feed_dict={net.bbox_biases: orig_1 * self.bbox_stds + self.bbox_means})

            if not os.path.exists(self.output_dir):
                os.makedirs(self.output_dir)

            infix = ('_' + cfg.TRAIN.SNAPSHOT_INFIX
                     if cfg.TRAIN.SNAPSHOT_INFIX != '' else '')
            filename = (cfg.TRAIN.SNAPSHOT_PREFIX + infix +
                        '_iter_{:d}'.format(iter+1) + '.ckpt')
            filename = os.path.join(self.output_dir, filename)

            self.saver.save(sess, filename)
            print 'Wrote snapshot to: {:s}'.format(filename)

            if cfg.TRAIN.BBOX_REG and net.layers.has_key('bbox_pred'):
                with tf.variable_scope('bbox_pred', reuse=True):
                    # restore net to original state
                    sess.run(net.bbox_weights_assign, feed_dict={net.bbox_weights: orig_0})
                    sess.run(net.bbox_bias_assign, feed_dict={net.bbox_biases: orig_1})

    def get_rnn_cell(self, hidden_size, rnn_mode='GRU'):
        if rnn_mode == 'BASIC':
            return tf.contrib.rnn.BasicLSTMCell(hidden_size)
        if rnn_mode == 'RNN':
            return tf.contrib.rnn.BasicRNNCell(hidden_size)
        if rnn_mode == 'BLOCK':
            return tf.contrib.rnn.LSTMBlockCell(
                hidden_size, forget_bias=0.0)
        if rnn_mode == 'GRU':
            return tf.contrib.rnn.GRUCell(hidden_size)
        raise ValueError("rnn_mode %s not supported" % rnn_mode)

    def build_RNN(self, inputs):
        """
        Encoder: Encode images, generate outputs and last hidden states
        :param encoder_inputs: inputs for all steps,shape=[batch_size, step, feature_size]
        :return: outputs of all steps and last hidden state
        """
        #input_list = tf.unstack(inputs, self.rnn_steps, 1)
        #input_dropout = [tf.nn.dropout(input_i, self.keep_prob) for input_i in inputs]
        cell = self.get_rnn_cell(self.hidden_size)
        outputs, states = rnn.static_rnn(cell, inputs, dtype=tf.float32)
        #cell_fw = self.get_rnn_cell(self.hidden_size)
        #cell_bw = self.get_rnn_cell(self.hidden_size)
        #outputs, states = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, inputs, sequence_length=[12], dtype=tf.float32)
        return outputs, states

    def faster_rcnn_loss(self, net):
        # RPN
        # classification loss
        rpn_cls_score = tf.reshape(net.get_output('rpn_cls_score_reshape'), [-1, 2])
        rpn_label = tf.reshape(net.get_output('rpn-data')[0], [-1])
        rpn_cls_score = tf.reshape(tf.gather(rpn_cls_score, tf.where(tf.not_equal(rpn_label, -1))), [-1, 2])
        rpn_label = tf.reshape(tf.gather(rpn_label, tf.where(tf.not_equal(rpn_label, -1))), [-1])
        rpn_cross_entropy = tf.reduce_mean(
            tf.nn.sparse_softmax_cross_entropy_with_logits(logits=rpn_cls_score, labels=rpn_label))

        # bounding box regression L1 loss
        rpn_bbox_pred = net.get_output('rpn_bbox_pred')
        rpn_bbox_targets = tf.transpose(net.get_output('rpn-data')[1], [0, 2, 3, 1])
        rpn_bbox_inside_weights = tf.transpose(net.get_output('rpn-data')[2], [0, 2, 3, 1])
        rpn_bbox_outside_weights = tf.transpose(net.get_output('rpn-data')[3], [0, 2, 3, 1])
        rpn_smooth_l1 = self._modified_smooth_l1(3.0, rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                                 rpn_bbox_outside_weights)
        rpn_loss_box = tf.reduce_mean(tf.reduce_sum(rpn_smooth_l1, reduction_indices=[1, 2, 3]))

        # R-CNN
        # classification loss
        cls_score = net.get_output('cls_score')
        label = tf.reshape(net.get_output('roi-data')[1], [-1])
        cross_entropy = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=cls_score, labels=label))

        # bounding box regression L1 loss
        bbox_pred = net.get_output('bbox_pred')
        bbox_targets = net.get_output('roi-data')[2]
        bbox_inside_weights = net.get_output('roi-data')[3]
        bbox_outside_weights = net.get_output('roi-data')[4]

        smooth_l1 = self._modified_smooth_l1(1.0, bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights)
        loss_box = tf.reduce_mean(tf.reduce_sum(smooth_l1, reduction_indices=[1]))

        return cross_entropy, loss_box, rpn_cross_entropy, rpn_loss_box

    def weight_variable(self,shape):
        initial = tf.truncated_normal(shape, stddev=0.1)
        return tf.Variable(initial)

    def bias_variable(self, shape):
        initial = tf.constant(0.1, shape = shape)
        return tf.Variable(initial)

    def train_model(self, sess, max_iters):
        """Network training loop."""

        data_layer = get_data_layer(self.roidb, self.imdb.num_classes)

        part_features_fc7 = self.net.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc71 = self.net1.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc72 = self.net2.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc73 = self.net3.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc74 = self.net4.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc75 = self.net5.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc76 = self.net6.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc77 = self.net7.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc78 = self.net8.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc79 = self.net9.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc710 = self.net10.get_output('pool_5')[:self.proposal_number, :]
        part_features_fc711 = self.net11.get_output('pool_5')[:self.proposal_number, :]

        # learning matrix 1
        Matrix_L1_S1 = tf.get_variable('L1_S1', [self.feature_size, self.feature_size],
                                       initializer=tf.random_normal_initializer(
                                           stddev=1 / math.sqrt(self.feature_size * self.feature_size)))
        # learning matrix 2
        Matrix_L1_S2 = tf.get_variable('L1_S2', [self.feature_size, self.feature_size],
                                       initializer=tf.random_normal_initializer(
                                           stddev=1 / math.sqrt(self.feature_size * self.feature_size)))

        ################################
        #### get the region feature ####
        ######### max pooling ##########
        ################################
        part_features_fc7 = tf.reduce_max(tf.reshape(part_features_fc7, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc71 = tf.reduce_max(tf.reshape(part_features_fc71, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc72 = tf.reduce_max(tf.reshape(part_features_fc72, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc73 = tf.reduce_max(tf.reshape(part_features_fc73, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc74 = tf.reduce_max(tf.reshape(part_features_fc74, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc75 = tf.reduce_max(tf.reshape(part_features_fc75, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc76 = tf.reduce_max(tf.reshape(part_features_fc76, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc77 = tf.reduce_max(tf.reshape(part_features_fc77, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc78 = tf.reduce_max(tf.reshape(part_features_fc78, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc79 = tf.reduce_max(tf.reshape(part_features_fc79, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc710 = tf.reduce_max(tf.reshape(part_features_fc710, [self.proposal_number, 49, 512]), axis=1)
        part_features_fc711 = tf.reduce_max(tf.reshape(part_features_fc711, [self.proposal_number, 49, 512]), axis=1)

        ##############################
        ######### L1_S1 ##############
        ##############################

        # view 0
        L1_S1_Similarity = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc7, Matrix_L1_S1),
                                                   tf.transpose(part_features_fc7)))
        similarity = tf.reduce_sum(L1_S1_Similarity, axis=0, keep_dims=True) / self.proposal_number
        similarity = tf.transpose(similarity)
        part_sum = tf.reduce_sum(tf.multiply(similarity, part_features_fc7), axis=0, keep_dims=True)

        # view 1
        L1_S1_Similarity1 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc71, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc71)))
        similarity1 = tf.reduce_sum(L1_S1_Similarity1, axis=0, keep_dims=True) / self.proposal_number
        similarity1 = tf.transpose(similarity1)
        part_sum1 = tf.reduce_sum(tf.multiply(similarity1, part_features_fc71), axis=0, keep_dims=True)

        # view 2
        L1_S1_Similarity2 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc72, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc72)))
        similarity2 = tf.reduce_sum(L1_S1_Similarity2, axis=0, keep_dims=True) / self.proposal_number
        similarity2 = tf.transpose(similarity2)
        part_sum2 = tf.reduce_sum(tf.multiply(similarity2, part_features_fc72), axis=0, keep_dims=True)

        # view 3
        L1_S1_Similarity3 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc73, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc73)))
        similarity3 = tf.reduce_sum(L1_S1_Similarity3, axis=0, keep_dims=True) / self.proposal_number
        similarity3 = tf.transpose(similarity3)
        part_sum3 = tf.reduce_sum(tf.multiply(similarity3, part_features_fc73), axis=0, keep_dims=True)

        # view 4
        L1_S1_Similarity4 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc74, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc74)))
        similarity4 = tf.reduce_sum(L1_S1_Similarity4, axis=0, keep_dims=True) / self.proposal_number
        similarity4 = tf.transpose(similarity4)
        part_sum4 = tf.reduce_sum(tf.multiply(similarity4, part_features_fc74), axis=0, keep_dims=True)

        # view 5
        L1_S1_Similarity5 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc75, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc75)))
        similarity5 = tf.reduce_sum(L1_S1_Similarity5, axis=0, keep_dims=True) / self.proposal_number
        similarity5 = tf.transpose(similarity5)
        part_sum5 = tf.reduce_sum(tf.multiply(similarity5, part_features_fc75), axis=0, keep_dims=True)

        # view 6
        L1_S1_Similarity6 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc76, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc76)))
        similarity6 = tf.reduce_sum(L1_S1_Similarity6, axis=0, keep_dims=True) / self.proposal_number
        similarity6 = tf.transpose(similarity6)
        part_sum6 = tf.reduce_sum(tf.multiply(similarity6, part_features_fc76), axis=0, keep_dims=True)

        # view 7
        L1_S1_Similarity7 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc77, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc77)))
        similarity7 = tf.reduce_sum(L1_S1_Similarity7, axis=0, keep_dims=True) / self.proposal_number
        similarity7 = tf.transpose(similarity7)
        part_sum7 = tf.reduce_sum(tf.multiply(similarity7, part_features_fc77), axis=0, keep_dims=True)

        # view 8
        L1_S1_Similarity8 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc78, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc78)))
        similarity8 = tf.reduce_sum(L1_S1_Similarity8, axis=0, keep_dims=True) / self.proposal_number
        similarity8 = tf.transpose(similarity8)
        part_sum8 = tf.reduce_sum(tf.multiply(similarity8, part_features_fc78), axis=0, keep_dims=True)

        # view 9
        L1_S1_Similarity9 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc79, Matrix_L1_S1),
                                                    tf.transpose(part_features_fc79)))
        similarity9 = tf.reduce_sum(L1_S1_Similarity9, axis=0, keep_dims=True) / self.proposal_number
        similarity9 = tf.transpose(similarity9)
        part_sum9 = tf.reduce_sum(tf.multiply(similarity9, part_features_fc79), axis=0, keep_dims=True)

        # view 10
        L1_S1_Similarity10 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc710, Matrix_L1_S1),
                                                     tf.transpose(part_features_fc710)))
        similarity10 = tf.reduce_sum(L1_S1_Similarity10, axis=0, keep_dims=True) / self.proposal_number
        similarity10 = tf.transpose(similarity10)
        part_sum10 = tf.reduce_sum(tf.multiply(similarity10, part_features_fc710), axis=0, keep_dims=True)

        # view 11
        L1_S1_Similarity11 = tf.nn.softmax(tf.matmul(tf.matmul(part_features_fc711, Matrix_L1_S1),
                                                     tf.transpose(part_features_fc711)))
        similarity11 = tf.reduce_sum(L1_S1_Similarity11, axis=0, keep_dims=True) / self.proposal_number
        similarity11 = tf.transpose(similarity11)
        part_sum11 = tf.reduce_sum(tf.multiply(similarity11, part_features_fc711), axis=0, keep_dims=True)

        # concat views
        view_parts = tf.concat([part_sum, part_sum1], axis=0)
        view_parts = tf.concat([view_parts, part_sum2], axis=0)
        view_parts = tf.concat([view_parts, part_sum3], axis=0)
        view_parts = tf.concat([view_parts, part_sum4], axis=0)
        view_parts = tf.concat([view_parts, part_sum5], axis=0)
        view_parts = tf.concat([view_parts, part_sum6], axis=0)
        view_parts = tf.concat([view_parts, part_sum7], axis=0)
        view_parts = tf.concat([view_parts, part_sum8], axis=0)
        view_parts = tf.concat([view_parts, part_sum9], axis=0)
        view_parts = tf.concat([view_parts, part_sum10], axis=0)
        view_parts = tf.concat([view_parts, part_sum11], axis=0)
        view_parts = tf.nn.l2_normalize(view_parts, 1)

        '''L1_S2'''
        L1_S2_Similarity = tf.nn.softmax(tf.matmul(tf.matmul(view_parts, Matrix_L1_S2),
                                                   tf.transpose(view_parts)))
        view_similarity = tf.reduce_sum(L1_S2_Similarity, axis=0, keep_dims=True) / self.views
        view_similarity = tf.transpose(view_similarity)
        view_sums = tf.reduce_sum(tf.multiply(view_similarity, view_parts), axis=0, keep_dims=True)

        view_sums = tf.nn.l2_normalize(view_sums, 1)
        #
        view_sums_extend = tf.tile(view_sums, [self.views, 1])
        views_input = tf.add(view_parts, view_sums_extend)

        view_extend = [views_input]
        view_sequence = tf.unstack(view_extend, self.rnn_steps, 1)

        ######RNN Part##########
        ########################
        ########################
        outputs, states = self.build_RNN(view_sequence)
        outputs = tf.reshape(outputs, [-1, self.views, self.hidden_size])
        model_feature = tf.reduce_max(outputs, 1)

        # classification layer
        # second attention part is related to the acutual classes
        w_init = tf.truncated_normal_initializer(stddev=0.1)
        b_init = tf.constant_initializer(0.1)
        fc2_w = tf.get_variable('fc2_w', [self.hidden_size, self.classes], dtype=tf.float32,
                                initializer=w_init)
        fc2_b = tf.get_variable('fc2_b', [self.classes], dtype=tf.float32, initializer=b_init)

        cls_logits = tf.matmul(model_feature, fc2_w) + fc2_b
        cls_prob = tf.nn.softmax(cls_logits)


        # initializing variables
        saver1 = tf.train.Saver(max_to_keep=150)
        self.saver = saver1
        sess.run(tf.global_variables_initializer())
        self.saver.restore(sess, self.pretrained_model)
        print('loaded: %s' % (self.pretrained_model))


        last_snapshot_iter = -1
        timer = Timer()
        sums = .0

        # class_ac_test = True
        # class_ac_test = False
        class_acc = np.zeros(self.classes, np.float32)
        cmatrix = np.zeros([self.classes, self.classes], np.float32)

        model_num = 1315
        classes_num = [5,100,100,20,100,50,50,5,100,30,50,100,50,100,100,100,100,100,5,50]
        cnum = [[5],[100],[100],[20],[100],[50],[50],[5],[100],[30],[50],[100],[50],[100],[100],[100],[100],[100],[5],[50]]

        for iter in range(model_num):
            # get one batch
            train_target = data_layer.netvlad_target()

            blobs = data_layer.forward()
            blobs1 = data_layer.forward()
            blobs2 = data_layer.forward()
            blobs3 = data_layer.forward()
            blobs4 = data_layer.forward()
            blobs5 = data_layer.forward()
            blobs6 = data_layer.forward()
            blobs7 = data_layer.forward()
            blobs8 = data_layer.forward()
            blobs9 = data_layer.forward()
            blobs10 = data_layer.forward()
            blobs11 = data_layer.forward()

            # Make one SGD update
            feed_dict={self.net.data: blobs['data'], self.net.im_info: blobs['im_info'], self.net.keep_prob: 1.0,
                       self.net1.data: blobs1['data'], self.net1.im_info: blobs1['im_info'], self.net1.keep_prob: 1.0,
                       self.net2.data: blobs2['data'], self.net2.im_info: blobs2['im_info'], self.net2.keep_prob: 1.0,
                       self.net3.data: blobs3['data'], self.net3.im_info: blobs3['im_info'], self.net3.keep_prob: 1.0,
                       self.net4.data: blobs4['data'], self.net4.im_info: blobs4['im_info'], self.net4.keep_prob: 1.0,
                       self.net5.data: blobs5['data'], self.net5.im_info: blobs5['im_info'], self.net5.keep_prob: 1.0,
                       self.net6.data: blobs6['data'], self.net6.im_info: blobs6['im_info'], self.net6.keep_prob: 1.0,
                       self.net7.data: blobs7['data'], self.net7.im_info: blobs7['im_info'], self.net7.keep_prob: 1.0,
                       self.net8.data: blobs8['data'], self.net8.im_info: blobs8['im_info'], self.net8.keep_prob: 1.0,
                       self.net9.data: blobs9['data'], self.net9.im_info: blobs9['im_info'], self.net9.keep_prob: 1.0,
                       self.net10.data: blobs10['data'], self.net10.im_info: blobs10['im_info'],self.net10.keep_prob: 1.0,
                       self.net11.data: blobs11['data'], self.net11.im_info: blobs11['im_info'],self.net11.keep_prob: 1.0}

            run_options = None
            run_metadata = None
            if cfg.TRAIN.DEBUG_TIMELINE:
                run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                run_metadata = tf.RunMetadata()
            timer.tic()
            test_acc = sess.run(cls_prob, feed_dict=feed_dict, options=run_options, run_metadata=run_metadata)
            timer.toc()
            cmatrix[np.argmax(train_target)][np.argmax(test_acc, axis=1)[0]] += 1.0
            if np.argmax(test_acc, axis=1)[0] == np.argmax(train_target):
                sums += 1.0
                class_acc[np.argmax(train_target)] += 1.0

            print('model id: %d' % iter, np.argmax(test_acc, axis=1)[0], np.argmax(train_target))

        print("Total accuracy: %f" % (sums / model_num))
        for i in range(20):
                print(cmatrix[i])
        print(cmatrix/cnum)
        for i in range(self.classes):
            print("the %d class:%f" % (i, class_acc[i] / classes_num[i]))

        print('class acc: %f' % (sum(class_acc / classes_num) / self.classes))
        '''
            timer.tic()
            L1_S0, L1_S1, L1_S2, L1_S3, L1_S4, L1_S5 = sess.run(rl1, feed_dict=feed_dict, options=run_options,
                                                                run_metadata=run_metadata)
            L1_S6, L1_S7, L1_S8, L1_S9, L1_S10, L1_S11 = sess.run(rl2, feed_dict=feed_dict, options=run_options,
                                                                  run_metadata=run_metadata)
            L2_S = sess.run(L1_S2_Similarity, feed_dict=feed_dict, options=run_options,
                            run_metadata=run_metadata)

            part_attention[iter, 0] = L1_S0
            part_attention[iter, 1] = L1_S1
            part_attention[iter, 2] = L1_S2
            part_attention[iter, 3] = L1_S3
            part_attention[iter, 4] = L1_S4
            part_attention[iter, 5] = L1_S5
            part_attention[iter, 6] = L1_S6
            part_attention[iter, 7] = L1_S7
            part_attention[iter, 8] = L1_S8
            part_attention[iter, 9] = L1_S9
            part_attention[iter, 10] = L1_S10
            part_attention[iter, 11] = L1_S11
            view_attention[iter] = L2_S

            timer.toc()
            print(iter)
        sio.savemat('/data/lxh/Models/fine-grained/attention/part_att_car1.mat', {'pt': part_attention})
        sio.savemat('/data/lxh/Models/fine-grained/attention/view_att_car1.mat', {'vt': view_attention})
        '''

def get_training_roidb(imdb):
    """Returns a roidb (Region of Interest database) for use in training."""
    if not cfg.TRAIN.USE_FLIPPED:
        print 'Appending horizontally-flipped training examples...'
        imdb.append_flipped_images()
        print 'done'

    print 'Preparing training data...'
    if cfg.TRAIN.HAS_RPN:
        if cfg.IS_MULTISCALE:
            gdl_roidb.prepare_roidb(imdb)
        else:
            rdl_roidb.prepare_roidb(imdb)
    else:
        rdl_roidb.prepare_roidb(imdb)
    print 'done'

    return imdb.roidb


def get_data_layer(roidb, num_classes):
    """return a data layer."""
    if cfg.TRAIN.HAS_RPN:
        if cfg.IS_MULTISCALE:
            layer = GtDataLayer(roidb)
        else:
            layer = RoIDataLayer(roidb, num_classes)
    else:
        layer = RoIDataLayer(roidb, num_classes)

    return layer

def filter_roidb(roidb):
    """Remove roidb entries that have no usable RoIs."""

    def is_valid(entry):
        # Valid images have:
        #   (1) At least one foreground RoI OR
        #   (2) At least one background RoI
        overlaps = entry['max_overlaps']
        # find boxes with sufficient overlap
        fg_inds = np.where(overlaps >= cfg.TRAIN.FG_THRESH)[0]
        # Select background RoIs as those within [BG_THRESH_LO, BG_THRESH_HI)
        bg_inds = np.where((overlaps < cfg.TRAIN.BG_THRESH_HI) &
                           (overlaps >= cfg.TRAIN.BG_THRESH_LO))[0]
        # image is only valid if such boxes exist
        valid = len(fg_inds) > 0 or len(bg_inds) > 0
        return valid

    num = len(roidb)
    filtered_roidb = [entry for entry in roidb if is_valid(entry)]
    num_after = len(filtered_roidb)
    print 'Filtered {} roidb entries: {} -> {}'.format(num - num_after,
                                                       num, num_after)
    return filtered_roidb


def train_net(sess1, network,network1,network2,network3,network4,network5,network6,network7,network8,network9,network10,network11, imdb, roidb, output_dir, pretrained_model=None, max_iters=40000):
    """Train a Fast R-CNN network."""
    #os.environ['CUDA_VISIBLE_DEVICES']='0,3'

    roidb = filter_roidb(roidb)
    #saver = tf.train.Saver(max_to_keep=100,write_version=saver_pb2.SaverDef.V1)
    saver = tf.train.Saver(max_to_keep=150)


    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.5
    sess1.close()
    with tf.Session(config=config) as sess:
        sw = SolverWrapper(sess, saver, network,network1,network2,network3,network4,network5,network6,network7,network8,network9,network10,network11, imdb, roidb, output_dir, pretrained_model=pretrained_model)

        print 'Solving...'
        sw.train_model(sess, max_iters)
        print 'done solving'
