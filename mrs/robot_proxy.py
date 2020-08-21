import argparse
import logging.config

from fmlib.models.robot import Robot as RobotModel
from fmlib.models.tasks import TransportationTask as Task
from ropod.structs.status import TaskStatus as TaskStatusConst

from mrs.allocation.bidder import Bidder
from mrs.config.configurator import Configurator
from mrs.config.params import get_config_params
from mrs.simulation.simulator import Simulator
from mrs.timetable.monitor import TimetableMonitorProxy
from mrs.timetable.timetable import Timetable

_component_modules = {'simulator': Simulator,
                      'timetable': Timetable,
                      'timetable_monitor': TimetableMonitorProxy,
                      'bidder': Bidder,
                      }


class RobotProxy:
    def __init__(self, robot_id, api, bidder, timetable_monitor, **kwargs):
        self.logger = logging.getLogger('mrs.robot.proxy%s' % robot_id)

        self.robot_id = robot_id
        self.api = api
        self.bidder = bidder
        self.timetable_monitor = timetable_monitor

        self.robot_model = RobotModel.create_new(robot_id, save=False)
        self.tasks = dict()
        self.tasks_status = dict()
        self.requests = dict()

        self.api.register_callbacks(self)
        self.logger.info("Initialized RobotProxy %s", robot_id)

    def configure(self, **kwargs):
        self.logger.debug("Configuring robot proxy")
        for component_name, component in self.__dict__.items():
            if hasattr(component, 'configure'):
                self.logger.debug("Configuring: %s", component_name)
                component.configure(robot=self.robot_model,
                                    tasks=self.tasks,
                                    tasks_status=self.tasks_status,
                                    **kwargs)

    def robot_pose_cb(self, msg):
        payload = msg.get("payload")
        if payload.get("robotId") == self.robot_id:
            self.logger.debug("Robot %s received pose", self.robot_id)
            self.robot_model.update_position(save=False, **payload.get("pose"))

    def task_cb(self, msg):
        payload = msg['payload']
        assigned_robots = payload.get("assignedRobots")
        if self.robot_id in assigned_robots:
            task = Task.from_payload(payload, save=False)
            self.tasks[task.task_id] = task
            self.tasks_status[task.task_id] = TaskStatusConst.DISPATCHED
            self.logger.debug("Received task %s", task.task_id)

    def run(self):
        try:
            self.api.start()
            while True:
                # time.sleep(0.1)
                pass
        except (KeyboardInterrupt, SystemExit):
            self.logger.info("Terminating %s robot ...", self.robot_id)
            self.api.shutdown()
            self.logger.info("Exiting...")


if __name__ == '__main__':
    from planner.planner import Planner

    parser = argparse.ArgumentParser()
    parser.add_argument('robot_id', type=str, help='example: robot_001')
    parser.add_argument('--file', type=str, action='store', help='Path to the config file')
    parser.add_argument('--experiment', type=str, action='store', help='Experiment_name')
    parser.add_argument('--approach', type=str, action='store', help='Approach name')
    args = parser.parse_args()

    config_params = get_config_params(args.file, experiment=args.experiment, approach=args.approach)
    config = Configurator(config_params, component_modules=_component_modules)
    components = config.config_robot_proxy(args.robot_id)

    robot = RobotProxy(**components, d_graph_watchdog=config_params.get("d_graph_watchdog"))
    robot.configure(planner=Planner(**config_params.get("planner")))
    robot.run()
