import argparse
import logging.config
import time

from mrs.allocate import Allocate
from mrs.config.params import get_config_params
from mrs.experiments.db.models.experiment import Experiment
from mrs.utils.utils import load_yaml_file
from mrs.utils.utils import load_yaml_file_from_module


class RunExperiment:
    def __init__(self, config_params, robot_poses, new_run=True):
        self.config_params = config_params
        self.experiment_name = config_params.get("experiment")
        self.approach = config_params.get("approach")
        self.robot_poses = robot_poses
        self.new_run = new_run

        self.dataset_module = config_params.get("dataset_module")
        self.datasets = config_params.get("datasets")

        self.logger = logging.getLogger('mrs.allocate')
        logger_config = self.config_params.get('logger')
        logging.config.dictConfig(logger_config)

        self.logger.info("Experiment: %s \n Approach: %s \n Dataset Module: %s\n Datasets: %s",
                         self.experiment_name,
                         self.approach,
                         self.dataset_module,
                         self.datasets)

    def start(self):
        for dataset in self.datasets:
            self.logger.info("Running experiment with dataset %s", dataset)
            self.run(dataset)

    def run(self, dataset):
        allocate = Allocate(self.config_params, self.robot_poses, self.dataset_module, dataset)
        try:
            allocate.start_allocation()
            while not allocate.terminated:
                print("Approx current time: ", allocate.simulator_interface.get_current_time())
                allocate.check_termination_test()
                time.sleep(0.5)

            Experiment.create_new(self.experiment_name, self.approach, dataset, self.new_run)
            allocate.terminate()

        except (KeyboardInterrupt, SystemExit):
            print('Task request test interrupted; exiting')
            allocate.terminate()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', type=str, action='store', help='Experiment_name')
    parser.add_argument('approach', type=str, action='store', help='Approach name')
    parser.add_argument('--file', type=str, action='store', help='Path to the config file')
    parser.add_argument('--new_run', type=bool, action='store', default=True,
                        help='If True a new run is added if False last run ' 'is repeated')
    args = parser.parse_args()

    config_params_ = get_config_params(args.file, experiment=args.experiment, approach=args.approach)

    robot_poses_module = config_params_.get("robot_poses_module")
    robot_poses_file = config_params_.get("robot_poses")
    robot_poses_ = load_yaml_file_from_module(robot_poses_module, robot_poses_file + ".yaml")

    run = RunExperiment(config_params_, robot_poses_, args.new_run)
    run.start()