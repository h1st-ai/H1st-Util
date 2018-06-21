from __future__ import division

import copy
import math
import os
import pandas
import psutil
import random

import arimo.backend
from arimo.blueprints.base import BlueprintedArimoDLModel, BlueprintedKerasModel, _docstr_blueprint
from arimo.blueprints.mixins.eval import RegrEvalMixIn
from arimo.blueprints.ts import _TimeSerDLSupervisedBlueprintABC
from arimo.df.from_files import ArrowADF
from arimo.df.spark_from_files import ArrowSparkADF
from arimo.dl.base import LossPlateauLrDecay
from arimo.util import fs, Namespace
from arimo.util.decor import _docstr_verbose
from arimo.util.dl import MASK_VAL
from arimo.util.pkl import pickle_able
import arimo.debug


@_docstr_blueprint
class DLBlueprint(RegrEvalMixIn, _TimeSerDLSupervisedBlueprintABC):
    _DEFAULT_PARAMS = \
        copy.deepcopy(
            _TimeSerDLSupervisedBlueprintABC._DEFAULT_PARAMS)

    _DEFAULT_PARAMS.update(
        model=Namespace(
            factory=Namespace(
                name='arimo.dl.experimental.keras.simple_lstm_regressor',
                n_timeser_lstm_nodes=68,
                n_fdfwd_hid_nodes=38),

            train=Namespace(
                objective=None   # *** DON'T IMPOSE MSE UP-FRONT AS IT'S OVER-SENSITIVE TO LARGE OUTLIERS ***
            )),

        __metadata__={
            'model': Namespace(
                label='Model Params'),

            'model.factory': Namespace(
                label='Model Factory Function'),

            'model.factory.name': Namespace(
                label='Model Factory Function',
                description='Full Name package.module.submodule.factory_func_name',
                type='string',
                choices=['arimo.dl.experimental.keras.simple_lstm_regressor'],
                default='arimo.dl.experimental.keras.simple_lstm_regressor',
                tags=[]),

            'model.factory.n_timeser_lstm_nodes': Namespace(
                label='No. of LSTM Hidden Cells',
                description='Number of Long Short-Term Memory Cells in Deep Learning Network',
                type='int',
                default=68,
                tags=[]),

            'model.factory.n_fdfwd_hid_nodes': Namespace(
                label='No. of Feed-Forward Hidden Nodes',
                description='Number of Hidden Feed-Forward Nodes in Deep Learning Network',
                type='int',
                default=38,
                tags=[]),

            'model.train': Namespace(
                label='Model Training Params'),

            'model.train.objective': Namespace(
                label='Model Training Objective')})

    @_docstr_verbose
    def train(self, df, **kwargs):
        __gen_queue_size__ = \
            kwargs.pop(
                '__gen_queue_size__',
                self.DEFAULT_MODEL_TRAIN_MAX_GEN_QUEUE_SIZE)
        assert __gen_queue_size__, \
            '*** __gen_queue_size__ = {} ***'.format(__gen_queue_size__)

        __n_workers__ = \
            kwargs.pop(
                '__n_workers__',
                self.DEFAULT_MODEL_TRAIN_N_WORKERS)
        assert __n_workers__, \
            '*** __n_workers__ = {} ***'.format(__n_workers__)

        __multiproc__ = kwargs.pop('__multiproc__', True)

        __n_gpus__ = \
            kwargs.pop(
                '__n_gpus__',
                self.DEFAULT_MODEL_TRAIN_N_GPUS)
        assert __n_gpus__, \
            '*** __n_gpus__ = {} ***'.format(__n_gpus__)

        __cpu_merge__ = bool(kwargs.pop('__cpu_merge__', True))
        __cpu_reloc__ = bool(kwargs.pop('__cpu_reloc__', False))   # *** cpu_relocation MAKES TEMPLATE MODEL WEIGHTS FAIL TO UPDATE ***

        # verbosity
        verbose = kwargs.pop('verbose', True)

        model = self.model(ver=self.params.model.ver)

        _lower_outlier_threshold_applicable = \
            pandas.notnull(self.params.data.label.lower_outlier_threshold)

        _upper_outlier_threshold_applicable = \
            pandas.notnull(self.params.data.label.upper_outlier_threshold)

        __excl_outliers__ = \
            self.params.data.label.excl_outliers and \
            self.params.data.label.outlier_tails and \
            self.params.data.label.outlier_tail_proportion and \
            (self.params.data.label.outlier_tail_proportion < .5) and \
            (_lower_outlier_threshold_applicable or _upper_outlier_threshold_applicable)

        if __excl_outliers__:
            _outlier_robust_condition = \
                ('BETWEEN {} AND {}'
                 .format(
                    self.params.data.label.lower_outlier_threshold,
                    self.params.data.label.upper_outlier_threshold)
                 if _upper_outlier_threshold_applicable
                 else '>= {}'.format(self.params.data.label.lower_outlier_threshold)) \
                    if _lower_outlier_threshold_applicable \
                    else '<= {}'.format(self.params.data.label.upper_outlier_threshold)

            if arimo.debug.ON:
                model.stdout_logger.debug(
                    msg='*** TRAIN: CONDITION ROBUST TO OUTLIER LABELS: {} {} ***\n'
                        .format(self.params.data.label.var, _outlier_robust_condition))

        adf = self.prep_data(
                df=df,
                __mode__=self._TRAIN_MODE,
                verbose=verbose,
                **kwargs)

        self._derive_model_train_params(
            data_size=
                (adf.approxNRows
                 if isinstance(adf, ArrowADF)
                 else adf.nRows)
                if self.params.model.train.n_samples_max_multiple_of_data_size
                else None)

        model.stdout_logger.info(
            'TRAINING:'
            '\n- Pred Vars Vec Size: {:,}'
            '\n- Train Samples: {:,}'
            '\n- Val Samples: {:,}'
            '\n- Epochs: {:,}'
            '\n- Train Samples/Epoch: {:,}'
            '\n- Train Batch Size: {:,}'
            '\n- Val Samples/Epoch: {:,}'
            '\n- Val Batch Size: {:,}'
            '\n- Generator Queue Size: {}'
            '\n- Processes/Threads: {}'
            '\n- Multi-Processing: {}'
            '\n- GPUs: {}{}{}'
            .format(
                self.params.data._prep_vec_size,
                self.params.model.train._n_train_samples,
                self.params.model.train._n_val_samples,
                self.params.model.train._n_epochs,
                self.params.model.train._n_train_samples_per_epoch,
                self.params.model.train.batch_size,
                self.params.model.train._n_val_samples_per_epoch,
                self.params.model.train.val_batch_size,
                __gen_queue_size__,
                __n_workers__,
                __multiproc__,
                __n_gpus__,
                ' (CPU Merge: {}; CPU Reloc: {}); Save Multi-GPU Model: {})'
                        .format(__cpu_merge__, __cpu_reloc__, arimo.debug.SAVE_MULTI_GPU_MODELS)
                    if __n_gpus__ > 1
                    else ''))

        feature_cols = self.params.data._cat_prep_cols + self.params.data._num_prep_cols

        filter = \
            {self.params.data.label.var:
                (self.params.data.label.lower_outlier_threshold,
                 self.params.data.label.upper_outlier_threshold)} \
            if __excl_outliers__ \
            else {}

        piece_paths = list(adf.piecePaths)
        random.shuffle(piece_paths)
        split_idx = int(math.ceil(self.params.model.train.train_proportion * adf.nPieces))
        train_piece_paths = piece_paths[:split_idx]
        val_piece_paths = piece_paths[split_idx:]

        if isinstance(model, BlueprintedArimoDLModel):
            assert isinstance(adf, ArrowADF)

            model.stdout_logger.info(model.config)

            n_threads = psutil.cpu_count(logical=True) - 2

            # TODO: TimeSer DataFeeder
            model.train_with_queue_reader_inputs(
                train_input=
                    adf._CrossSectDLDF(
                        feature_cols,
                        self.params.data.label.var,
                        piecePaths=train_piece_paths,
                        n=self.params.model.train.batch_size,
                        filter=filter,
                        nThreads=n_threads,
                        isRegression=True),

                val_input=
                    adf._CrossSectDLDF(
                        feature_cols,
                        self.params.data.label.var,
                        piecePaths=val_piece_paths,
                        n=self.params.model.train.val_batch_size,
                        filter=filter,
                        nThreads=n_threads,
                        isRegression=True),

                lr_scheduler=
                    LossPlateauLrDecay(
                        learning_rate=model.config.learning_rate,
                        decay_rate=model.config.lr_decay,
                        patience=self.params.model.train.reduce_lr_on_plateau.patience_n_epochs),

                max_epoch=self.params.model.train._n_epochs,

                early_stopping_patience=
                    max(self.params.model.train.early_stop.patience_min_n_epochs,
                        int(math.ceil(self.params.model.train.early_stop.patience_proportion_total_n_epochs *
                                      self.params.model.train._n_epochs))),

                num_train_batches_per_epoch=self.params.model.train._n_train_batches_per_epoch,

                num_test_batches_per_epoch=self.params.model.train._n_val_batches_per_epoch)

        else:
            assert isinstance(model, BlueprintedKerasModel)

            assert isinstance(adf, (ArrowADF, ArrowSparkADF))

            model.summary()

            if __n_gpus__ > 1:
                _model_to_fit = \
                    arimo.backend.keras.utils.multi_gpu_model(
                        model._obj,
                        gpus=__n_gpus__,
                        cpu_merge=__cpu_merge__,
                        cpu_relocation=__cpu_reloc__)

                if arimo.debug.SAVE_MULTI_GPU_MODELS:
                    model._obj = _model_to_fit

            else:
                _model_to_fit = model

            _model_to_fit.compile(
                loss=self.params.model.train.objective
                    if self.params.model.train.objective
                    else 'MAE',   # mae / mean_absolute_error (more resilient to outliers)
                optimizer=arimo.backend.keras.optimizers.Nadam(),
                metrics=[# 'MSE',   # mean_squared_error,
                         # 'MAPE'   # mean_absolute_percentage_error
                ])

            fs.mkdir(
                dir=model.dir,
                hdfs=False)

            open(os.path.join(
                    model.dir,
                    self.params.model._persist.struct_file), 'w') \
                .write(model.to_json())

            n_threads = int(math.ceil(psutil.cpu_count(logical=True) / __n_workers__))

            train_gen = \
                adf.gen(
                    feature_cols +
                        (- self.params.pred_horizon_len - self.params.max_input_ser_len + 1,
                         - self.params.pred_horizon_len),
                    self.params.data.label.var,
                    piecePaths=train_piece_paths,
                    n=__n_gpus__ * self.params.model.train.batch_size,
                    withReplacement=False,
                    seed=None,
                    anon=True,
                    collect='numpy',
                    pad=MASK_VAL,
                    filter=filter,
                    nThreads=n_threads)

            assert pickle_able(train_gen)

            val_gen = \
                adf.gen(
                    feature_cols +
                        (- self.params.pred_horizon_len - self.params.max_input_ser_len + 1,
                         - self.params.pred_horizon_len),
                    self.params.data.label.var,
                    piecePaths=val_piece_paths,
                    n=__n_gpus__ * self.params.model.train.val_batch_size,
                    withReplacement=False,
                    seed=None,
                    anon=True,
                    collect='numpy',
                    pad=MASK_VAL,
                    filter=filter,
                    nThreads=n_threads)

            assert pickle_able(val_gen)

            model.history = \
                _model_to_fit.fit_generator(
                    generator=train_gen(),
                        # a generator.
                        # The output of the generator must be either a tuple(inputs, targets)
                        # or a tuple(inputs, targets, sample_weights).
                        # Therefore, all arrays in this tuple must have the same length (equal to the size of this batch).
                        # Different batches may have different sizes.
                        # For example, the last batch of the epoch is commonly smaller than the others,
                        # if the size of the dataset is not divisible by the batch size.
                        # The generator is expected to loop over its data indefinitely.
                        # An epoch finishes when steps_per_epoch batches have been seen by the model.

                    steps_per_epoch=self.params.model.train._n_train_batches_per_epoch,
                        # Total number of steps (batches of samples) to yield from generator
                        # before declaring one epoch finished and starting the next epoch.
                        # It should typically be equal to the number of samples of your dataset divided by the batch size.

                    epochs=self.params.model.train._n_epochs,
                        # Integer, total number of iterations on the data.
                        # Note that in conjunction with initial_epoch, the parameter epochs is to be understood as "final epoch".
                        # The model is not trained for n steps given by epochs, but until the epoch epochs is reached.

                    verbose=1,   # progress bar, because TS data feeding may be slow
                        # Integer. 0, 1, or 2. Verbosity mode. 0 = silent, 1 = progress bar, 2 = one line per epoch.

                    callbacks=[   # list of callbacks to be called during training.
                        # BaseLogger(stateful_metrics=None),
                            # Callback that accumulates epoch averages of metrics.
                            # This callback is automatically applied to every Keras model.

                        # *** DISABLED BECAUSE PROGRESS IS ALREADY DISPLAYED WHEN verbose = True ***
                        # ProgbarLogger(count_mode='samples', stateful_metrics=None),
                            # Callback that prints metrics to stdout.

                        # History(),
                            # Callback that records events into a History object.
                            # This callback is automatically applied to every Keras model.
                            # The History object gets returned by the fit method of models.

                        arimo.backend.keras.callbacks.TerminateOnNaN(),
                            # Callback that terminates training when a NaN loss is encountered.

                        arimo.backend.keras.callbacks.ModelCheckpoint(
                            # Save the model after every epoch.

                            filepath=os.path.join(model.dir, self.params.model._persist.weights_file),
                                # string, path to save the model file.

                            monitor=self.params.model.train.val_metric.name,
                                # quantity to monitor.

                            verbose=int(verbose > 0),
                                # verbosity mode, 0 or 1

                            save_best_only=(self.params.model.train.min_n_val_samples_per_epoch == 'all'),
                                # if save_best_only=True,
                                # the latest best model according to the quantity monitored will not be overwritten.

                            save_weights_only=True,
                                # if True, then only the model's weights will be saved (model.save_weights(filepath)),
                                # else the full model is saved (model.save(filepath))

                            mode=self.params.model.train.val_metric.mode,
                                # one of {auto, min, max}.
                                # If save_best_only=True, the decision to overwrite the current save file is made
                                # based on either the maximization or the minimization of the monitored quantity.
                                # For val_acc, this should be max, for val_loss this should be min, etc.
                                # In auto mode, the direction is automatically inferred from the name of the monitored quantity.

                            period=1
                                # Interval (number of epochs) between checkpoints.
                            ),

                        arimo.backend.keras.callbacks.ReduceLROnPlateau(
                            # Reduce learning rate when a metric has stopped improving.
                            # Models often benefit from reducing the learning rate by a factor of 2-10 once learning stagnates.
                            # This callback monitors a quantity and if no improvement is seen for a 'patience' number of epochs,
                            # the learning rate is reduced.

                            monitor=self.params.model.train.val_metric.name,
                                # quantity to be monitored.

                            factor=self.params.model.train.reduce_lr_on_plateau.factor,
                                # factor by which the learning rate will be reduced. new_lr = lr * factor

                            patience=self.params.model.train.reduce_lr_on_plateau.patience_n_epochs,
                                # number of epochs with no improvement after which learning rate will be reduced.

                            verbose=int(verbose > 0),
                                # int. 0: quiet, 1: update messages.

                            mode=self.params.model.train.val_metric.mode,
                                # one of {auto, min, max}.
                                # In min mode, lr will be reduced when the quantity monitored has stopped decreasing;
                                # In max mode it will be reduced when the quantity monitored has stopped increasing;
                                # In auto mode, the direction is automatically inferred from the name of the monitored quantity.

                            min_delta=self.params.model.train.val_metric.significance,
                                # threshold for measuring the new optimum, to only focus on significant changes.

                            cooldown=0,
                                # number of epochs to wait before resuming normal operation after lr has been reduced.

                            min_lr=0
                                # lower bound on the learning rate.
                            ),

                        arimo.backend.keras.callbacks.EarlyStopping(
                            # Stop training when a monitored quantity has stopped improving.

                            monitor=self.params.model.train.val_metric.name,
                                # quantity to be monitored.

                            min_delta=self.params.model.train.val_metric.significance,
                                # minimum change in the monitored quantity to qualify as an improvement,
                                # i.e. an absolute change of less than min_delta, will count as no improvement.

                            patience=max(self.params.model.train.early_stop.patience_min_n_epochs,
                                         int(math.ceil(self.params.model.train.early_stop.patience_proportion_total_n_epochs *
                                                       self.params.model.train._n_epochs))),
                                # number of epochs with no improvement after which training will be stopped.

                            verbose=int(verbose),
                                # verbosity mode.

                            mode=self.params.model.train.val_metric.mode,
                                # one of {auto, min, max}.
                                # In min mode, training will stop when the quantity monitored has stopped decreasing;
                                # In max mode it will stop when the quantity monitored has stopped increasing;
                                # In auto mode, the direction is automatically inferred from the name of the monitored quantity.

                            baseline=None
                                # Baseline value for the monitored quantity to reach.
                                # Training will stop if the model doesn't show improvement over the baseline.
                            )],

                    validation_data=val_gen(),
                        # this can be either:
                        # - a generator for the validation data;
                        # - a tuple(inputs, targets); or
                        # - a tuple(inputs, targets, sample_weights).

                    validation_steps=self.params.model.train._n_val_batches_per_epoch,
                        # Only relevant if validation_data is a generator.
                        # Total number of steps (batches of samples) to yield from  validation_data generator
                        # before stopping at the end of every epoch.
                        # It should typically be equal to the number of samples of your validation dataset divided by the batch size.

                    class_weight={},
                        # Optional dictionary mapping class indices (integers) to a weight (float) value,
                        # used for weighting the loss function (during training only).
                        # This can be useful to tell the model to "pay more attention" to samples from an under-represented class.

                    max_queue_size=__gen_queue_size__,
                        # Integer. Maximum size for the generator queue.
                        # If unspecified, max_queue_size will default to 10

                    workers=__n_workers__,
                        # Integer. Maximum number of processes to spin up when using process-based threading.
                        # If unspecified, workers will default to 1. If 0, will execute the generator on the main thread.

                    use_multiprocessing=__multiproc__,
                        # Boolean. If True, use process-based threading.
                        # If unspecified, use_multiprocessing will default to False.
                        # Note that because this implementation relies on multiprocessing,
                        # you should not pass non-picklable arguments to the generator
                        # as they can't be passed easily to children processes.

                    shuffle=False,
                        # Boolean (whether to shuffle the order of the batches at the beginning of each epoch
                        # Only used with instances of Sequence (keras.utils.Sequence).
                        # Has no effect when steps_per_epoch is not None.

                    initial_epoch=0
                        # epoch at which to start training (useful for resuming a previous training run)
                    ) \
                .history

        model.save()

        return model
