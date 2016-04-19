import collections
import glob
import logging
import os
import time
import timeit
from contextlib import contextmanager

import nibabel as nib
import numpy as np
from six import string_types

from mdt import __version__
from mdt.IO import Nifti
from mdt.batch_utils import batch_profile_factory, AllSubjects
from mdt.components_loader import get_model
from mdt.configuration import config
from mdt.models.cascade import DMRICascadeModelInterface
from mdt.protocols import write_protocol
from mdt.utils import create_roi, load_problem_data, ProtocolProblemError, MetaOptimizerBuilder, get_cl_devices, \
    get_model_config, apply_model_protocol_options, model_output_exists, split_image_path, get_processing_strategy, \
    estimate_noise_std, FittingProcessingWorker, per_model_logging_context, recursive_merge_dict
from mot.load_balance_strategies import EvenDistribution
import mot.configuration
from mot.configuration import RuntimeConfigurationAction

__author__ = 'Robbert Harms'
__date__ = "2015-05-01"
__maintainer__ = "Robbert Harms"
__email__ = "robbert.harms@maastrichtuniversity.nl"


class BatchFitting(object):

    def __init__(self, data_folder, batch_profile=None, subjects_selection=None, recalculate=False,
                 cl_device_ind=None, double_precision=False):
        """This class is meant to make running computations as simple as possible.

        The idea is that a single folder is enough to fit_model the computations. One can optionally give it the
        batch_profile to use for the fitting. If not given, this class will attempt to use the
        batch_profile that fits the data folder best.

        For configuration of the optimizers uses the users configuration file. For batch fitting specific options use
        the options parameter.

        The general optimization options are loaded in this order:
            0) default options
            1) options from the batch profile

        Setting the cl_device_ind has the side effect that it changes the current run time cl_device settings in the
        MOT toolkit.

        Args:
            data_folder (str): the main directory to look for items to process.
            batch_profile (BatchProfile class or str): the batch profile to use or the name of a batch
                profile to load from the users folder.
            subjects_selection (BatchSubjectSelection): the subjects to use for processing.
                If None all subjects are processed.
            recalculate (boolean): If we want to recalculate the results if they are already present.
            cl_device_ind (int): the index of the CL device to use. The index is from the list from the function
                get_cl_devices().
            double_precision (boolean): if we would like to do the calculations in double precision
        """
        self._logger = logging.getLogger(__name__)
        self._batch_profile = batch_profile_factory(batch_profile, data_folder)
        self._subjects_selection = subjects_selection or AllSubjects()
        self._models_to_fit = self._batch_profile.get_models_to_fit()
        self._cl_device_ind = cl_device_ind
        self._recalculate = recalculate
        self._double_precision = double_precision

        if self._batch_profile is None:
            raise RuntimeError('No suitable batch profile could be '
                               'found for the directory {0}'.format(os.path.abspath(data_folder)))

        self._model_protocol_options = self._batch_profile.get_model_protocol_options()

        self._logger.info('Using MDT version {}'.format(__version__))
        self._logger.info('Using batch profile: {0}'.format(self._batch_profile))
        self._subjects = self._subjects_selection.get_selection(self._batch_profile.get_subjects())

        self._logger.info('Subjects found: {0}'.format(self._batch_profile.get_subjects_count()))
        self._logger.info('Subjects to process: {0}'.format(len(self._subjects)))

        if self._cl_device_ind is not None:
            mot.configuration.set_cl_environments([get_cl_devices()[self._cl_device_ind]])

    def get_all_subjects_info(self):
        """Get a dictionary with the info of all the found subjects.

        This will return information about all the subjects found and will disregard parameter 'subjects'
        that limits the amount of subjects we will run.

        Returns:
            list of batch_utils.SubjectInfo: information about all available subjects
        """
        return self._batch_profile.get_subjects()

    def get_subjects_info(self):
        """Get a dictionary with the info of the subject we will run computations on.

        This will return information about only the subjects that we will use in the batch fitting.

        Returns:
            list of batch_utils.SubjectInfo: information about all subjects we will use
        """
        return self._subjects

    def run(self):
        """Run the computations on the current dir with all the configured options. """
        self._logger.info('Running computations on {0} subjects'.format(len(self._subjects)))

        run_func = _BatchFitRunner(self._model_protocol_options, self._models_to_fit,
                                   self._recalculate, self._cl_device_ind, self._double_precision)
        list(map(run_func, self._subjects))

        return self._subjects


class _BatchFitRunner(object):

    def __init__(self, model_protocol_options, models_to_fit, recalculate, cl_device_ind, double_precision):
        self._model_protocol_options = model_protocol_options
        self._models_to_fit = models_to_fit
        self._recalculate = recalculate
        self._cl_device_ind = cl_device_ind
        self._double_precision = double_precision

    def __call__(self, subject_info):
        """Run the batch fitting on the given subject.

        This is a module level function to allow for python multiprocessing to work.

        Args:
            batch_instance (dict): contains the items: 'subject', 'config', 'output_dir'
        """
        logger = logging.getLogger(__name__)

        output_dir = subject_info.output_dir

        protocol = subject_info.get_protocol_loader().get_protocol()
        brain_mask_fname = subject_info.get_mask_filename()

        if all(model_output_exists(model, os.path.join(output_dir, split_image_path(brain_mask_fname)[1]))
               for model in self._models_to_fit) and not self._recalculate:
            logger.info('Skipping subject {0}, output exists'.format(subject_info.subject_id))
            return

        logger.info('Loading the data (DWI, mask and protocol) of subject {0}'.format(subject_info.subject_id))
        problem_data = load_problem_data(subject_info.get_dwi_info(), protocol, brain_mask_fname,
                                         dtype=np.float64 if self._double_precision else np.float32)

        write_protocol(protocol, os.path.join(output_dir, 'used_protocol.prtcl'))

        gradient_deviations = subject_info.get_gradient_deviations()
        if gradient_deviations:
            gradient_deviations = nib.load(gradient_deviations).get_data()

        noise_std = self._get_noise_std(subject_info.get_noise_std(), problem_data)

        start_time = timeit.default_timer()
        for model in self._models_to_fit:
            logger.info('Going to fit model {0} on subject {1}'.format(model, subject_info.subject_id))
            try:
                model_fit = ModelFit(model,
                                     problem_data,
                                     os.path.join(output_dir, split_image_path(brain_mask_fname)[1]),
                                     recalculate=self._recalculate,
                                     only_recalculate_last=True,
                                     model_protocol_options=self._model_protocol_options,
                                     cl_device_ind=self._cl_device_ind,
                                     double_precision=self._double_precision,
                                     gradient_deviations=gradient_deviations,
                                     noise_std=noise_std)
                model_fit.run()
            except ProtocolProblemError as ex:
                logger.info('Could not fit model {0} on subject {1} '
                            'due to protocol problems. {2}'.format(model, subject_info.subject_id, ex))
            else:
                logger.info('Done fitting model {0} on subject {1}'.format(model, subject_info.subject_id))
        logger.info('Fitted all models on subject {0} in time {1} (h:m:s)'.format(
            subject_info.subject_id, time.strftime('%H:%M:%S', time.gmtime(timeit.default_timer() - start_time))))

    def _get_noise_std(self, noise_std, problem_data):
        if noise_std == 'auto':
            logger = logging.getLogger(__name__)
            logger.info('The noise std was set to \'auto\', we will estimate one.')
            return estimate_noise_std(problem_data)
        elif noise_std is None:
            noise_std = 1.0
        return noise_std


class ModelFit(object):

    def __init__(self, model, problem_data, output_folder, optimizer=None,
                 recalculate=False, only_recalculate_last=False, model_protocol_options=None,
                 use_model_protocol_options=True,
                 cl_device_ind=None, double_precision=False, gradient_deviations=None, noise_std=None):
        """Setup model fitting for the given input model and data.

        To actually fit the model call run().

        Args:
            model (AbstractModel): An implementation of an AbstractModel that contains the model we want to optimize.
            problem_data (ProblemData): the problem data object which contains the dwi image, the dwi header, the
                brain_mask and the protocol to use.
            output_folder (string): The full path to the folder where to place the output
            optimizer (AbstractOptimizer): The optimization routine to use. If None, we create one using the
                configuration files.
            recalculate (boolean): If we want to recalculate the results if they are already present.
            only_recalculate_last (boolean):
                This is only of importance when dealing with CascadeModels.
                If set to true we only recalculate the last element in the chain
                    (if recalculate is set to True, that is).
                If set to false, we recalculate everything. This only holds for the first level of the cascade.
            model_protocol_options (dict): specific model protocol options to use during fitting.
                This is for example used during batch fitting to limit the protocol for certain models.
                For instance, in the Tensor model we generally only want to use the lower b-values, or for S0 only
                the unweighted. Please note that this is merged with the options defined in the config file.
            use_model_protocol_options (boolean): if we want to use the model protocol options or not.
            cl_device_ind (int): the index of the CL device to use. The index is from the list from the function
                get_cl_devices(). This can also be a list of device indices.
            double_precision (boolean): if we would like to do the calculations in double precision
            gradient_deviations (ndarray): set of gradient deviations to use. In HCP WUMINN format.
            noise_std (double or 'auto'): the noise level standard deviation. This is useful for model comparisons.
                By default this is None and we set it to 1. If set to auto we try to estimate it using multiple
                noise std calculators.
        """
        if isinstance(model, string_types):
            model = get_model(model)

        model.double_precision = double_precision

        self._model = model
        self._problem_data = problem_data
        self._output_folder = output_folder
        self._optimizer = optimizer
        self._recalculate = recalculate
        self._only_recalculate_last = only_recalculate_last
        self._model_protocol_options = recursive_merge_dict(config.get('model_protocol_options', {}),
                                                            model_protocol_options)
        self._use_model_protocol_options = use_model_protocol_options
        self._logger = logging.getLogger(__name__)
        self._cl_device_indices = cl_device_ind
        self._model_names_list = []

        if gradient_deviations is not None:
            self._logger.info('Using given gradient deviations.')
            model.set_gradient_deviations(gradient_deviations)

        if self._cl_device_indices is not None and not isinstance(self._cl_device_indices, collections.Iterable):
            self._cl_device_indices = [self._cl_device_indices]

        self._cl_envs = None
        self._load_balancer = None
        if self._cl_device_indices is not None:
            all_devices = get_cl_devices()
            self._cl_envs = [all_devices[ind] for ind in self._cl_device_indices]
            self._load_balancer = EvenDistribution()

        with mot.configuration.config_context(RuntimeConfigurationAction(cl_environments=self._cl_envs,
                                                                         load_balancer=self._load_balancer)):
            self._noise_std = self._get_noise_std(noise_std)

        if not model.is_protocol_sufficient(self._problem_data.protocol):
            raise ProtocolProblemError(
                'The given protocol is insufficient for this model. '
                'The reported errors where: {}'.format(self._model.get_protocol_problems(self._problem_data.protocol)))

    def run(self):
        """Run the model and return the resulting maps

        If we will not recalculate and the maps already exists, we will load the maps from file and return those.

        Returns:
            The result maps for the model we are running.
        """
        return self._run(self._model, self._recalculate, self._only_recalculate_last, {})

    def _run(self, model, recalculate, only_recalculate_last, meta_optimizer_config):
        """Recursively calculate the (cascade) models

        Args:
            model: The model to fit, if cascade we recurse
            recalculate (boolean): if we recalculate
            only_recalculate_last: if we recalculate, if we only recalculate the last item in the first cascade
            meta_optimizer_config: optional optimization configuration.
        """
        self._model_names_list.append(model.name)

        if isinstance(model, DMRICascadeModelInterface):
            results = {}
            last_result = None
            while model.has_next():
                sub_model = model.get_next(results)

                sub_recalculate = False
                if recalculate:
                    if only_recalculate_last:
                        if not model.has_next():
                            sub_recalculate = True
                    else:
                        sub_recalculate = True

                new_results = self._run(sub_model, sub_recalculate, recalculate, meta_optimizer_config)
                results.update({sub_model.name: new_results})
                last_result = new_results
                self._model_names_list.pop()

            model.reset()
            return last_result

        return self._run_single_model(model, recalculate, meta_optimizer_config, self._model_names_list)

    def _run_single_model(self, model, recalculate, meta_optimizer_config, model_names):
        with mot.configuration.config_context(RuntimeConfigurationAction(cl_environments=self._cl_envs,
                                                                         load_balancer=self._load_balancer)):
            with per_model_logging_context(os.path.join(self._output_folder, model.name)):
                self._logger.info('Using MDT version {}'.format(__version__))
                self._logger.info('Preparing for model {0}'.format(model.name))
                self._logger.info('Current cascade: {0}'.format(model_names))
                self._logger.info('Setting the noise standard deviation to {0}'.format(self._noise_std))
                model.evaluation_model.set_noise_level_std(self._noise_std, fix=True)

                optimizer = self._optimizer or MetaOptimizerBuilder(meta_optimizer_config).construct(model_names)

                if self._cl_device_indices is not None:
                    all_devices = get_cl_devices()
                    optimizer.cl_environments = [all_devices[ind] for ind in self._cl_device_indices]
                    optimizer.load_balancer = EvenDistribution()

                if self._use_model_protocol_options:
                    model_protocol_options = get_model_config(model_names, self._model_protocol_options)
                    problem_data = apply_model_protocol_options(model_protocol_options, self._problem_data)
                else:
                    problem_data = self._problem_data

                processing_strategy = get_processing_strategy('optimization', model_names)

                fitter = SingleModelFit(model, problem_data, self._output_folder, optimizer, processing_strategy,
                                        recalculate=recalculate)
                results = fitter.run()

        return results

    def _get_noise_std(self, noise_std):
        if noise_std == 'auto':
            self._logger.info('The noise std was set to \'auto\', we will estimate one.')
            return estimate_noise_std(self._problem_data)
        elif noise_std is None:
            noise_std = 1.0
        return noise_std


class SingleModelFit(object):

    def __init__(self, model, problem_data, output_folder, optimizer, processing_strategy, recalculate=False):
        """Fits a single model.

         This does not accept cascade models. Please use the more general ModelFit class for single and cascade models.

         Args:
             model (AbstractModel): An implementation of an AbstractModel that contains the model we want to optimize.
             problem_data (DMRIProblemData): The problem data object with which the model is initialized before running
             output_folder (string): The full path to the folder where to place the output
             optimizer (AbstractOptimizer): The optimization routine to use.
             processing_strategy (ModelProcessingStrategy): the processing strategy to use
             recalculate (boolean): If we want to recalculate the results if they are already present.

         Attributes:
             recalculate (boolean): If we want to recalculate the results if they are already present.
         """
        self.recalculate = recalculate

        self._model = model
        self._problem_data = problem_data
        self._output_folder = output_folder
        self._output_path = os.path.join(self._output_folder, self._model.name)
        self._optimizer = optimizer
        self._logger = logging.getLogger(__name__)
        self._processing_strategy = processing_strategy

        if not self._model.is_protocol_sufficient(problem_data.protocol):
            raise ProtocolProblemError(
                'The given protocol is insufficient for this model. '
                'The reported errors where: {}'.format(self._model.get_protocol_problems(problem_data.protocol)))

    def run(self):
        """Fits a single model.

        This will use the current ModelProcessingStrategy to do the actual optimization.
        """
        with per_model_logging_context(self._output_path):
            self._model.set_problem_data(self._problem_data)

            if self.recalculate:
                if os.path.exists(self._output_path):
                    list(map(os.remove, glob.glob(os.path.join(self._output_path, '*.nii*'))))
            else:
                if model_output_exists(self._model, self._output_folder):
                    maps = Nifti.read_volume_maps(self._output_path)
                    self._logger.info('Not recalculating {} model'.format(self._model.name))
                    return create_roi(maps, self._problem_data.mask)

            if not os.path.exists(self._output_path):
                os.makedirs(self._output_path)

            with self._logging():
                results = self._processing_strategy.run(self._model, self._problem_data,
                                                        self._output_path, self.recalculate,
                                                        FittingProcessingWorker(self._optimizer))
                self._write_protocol()

        return results

    def _write_protocol(self):
        write_protocol(self._problem_data.protocol, os.path.join(self._output_path, 'used_protocol.prtcl'))

    @contextmanager
    def _logging(self):
        """Adds logging information around the processing."""
        minimize_start_time = timeit.default_timer()
        self._logger.info('Fitting {} model'.format(self._model.name))

        yield

        run_time = timeit.default_timer() - minimize_start_time
        run_time_str = time.strftime('%H:%M:%S', time.gmtime(run_time))
        self._logger.info('Fitted {0} model with runtime {1} (h:m:s).'.format(self._model.name, run_time_str))
