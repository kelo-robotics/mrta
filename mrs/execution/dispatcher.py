import copy
import logging
from datetime import timedelta

from fmlib.models.actions import GoTo
from mrs.simulation.simulator import SimulatorInterface
from ropod.structs.status import TaskStatus as TaskStatusConst


class Dispatcher(SimulatorInterface):

    def __init__(self, timetable_manager, freeze_window, n_queued_tasks, fleet_monitor, **kwargs):
        """ Dispatches tasks to a multi-robot system based on temporal constraints

        Args:

            timetable_manager (TimetableManager): contains the timetables of all the robots in the fleet
            freeze_window (float): Defines the time (minutes) within which a task can be scheduled
                        e.g, with a freeze window of 2 minutes, a task can be scheduled if its earliest
                        start navigation time is within the next 2 minutes.
            kwargs:
                api (API): object that provides middleware functionality
                robot_store (robot_store): interface to interact with the db
        """
        simulator = kwargs.get('simulator')
        super().__init__(simulator)

        self.logger = logging.getLogger('mrs.dispatcher')
        self.api = kwargs.get('api')
        self.ccu_store = kwargs.get('ccu_store')

        self.timetable_manager = timetable_manager
        self.freeze_window = timedelta(minutes=freeze_window)
        self.n_queued_tasks = n_queued_tasks
        self.fleet_monitor = fleet_monitor

        self.robot_ids = list()
        self.d_graph_updates = dict()
        self.tasks = dict()

        self.logger.debug("Dispatcher started")

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            self.logger.debug("Adding %s", key)
            self.__dict__[key] = value

    def register_robot(self, robot):
        self.logger.debug("Registering robot %s", robot.robot_id)
        self.robot_ids.append(robot.robot_id)

    def run(self, **kwargs):
        self.dispatch_tasks()

    def is_schedulable(self, start_time):
        current_time = self.get_current_timestamp()
        if start_time.get_difference(current_time) < self.freeze_window:
            return True
        return False

    def get_robot_location(self, pose):
        """ Returns the name of the node in the map where the robot is located"""
        try:
            robot_location = self.planner.get_node(pose.x, pose.y)
        except AttributeError:
            self.logger.warning("No planner configured")
            # For now, return a known area
            robot_location = "AMK_D_L-1_C39"
        return robot_location

    def get_path(self, source, destination):
        try:
            return self.planner.get_path(source, destination)
        except AttributeError:
            self.logger.warning("No planner configured")

    def get_path_estimated_duration(self, path):
        try:
            mean, variance = self.planner.get_estimated_duration(path)
        except AttributeError:
            self.logger.warning("No planner configured")
            mean = 1
            variance = 0.1
        return mean, variance

    def get_pre_task_action(self, task, robot_id):
        pose = self.fleet_monitor.get_robot_pose(robot_id)
        robot_location = self.get_robot_location(pose)
        path = self.get_path(robot_location, task.request.start_location)
        mean, variance = self.get_path_estimated_duration(path)
        action = GoTo.create_new(type="ROBOT-TO-PICKUP", locations=path)
        action.update_duration(mean, variance)
        return action

    def add_pre_task_action(self, task, robot_id, timetable):
        self.logger.debug("Adding pre_task_action to plan for task %s", task.task_id)
        pre_task_action = self.get_pre_task_action(task, robot_id)
        task.plan[0].actions.insert(0, pre_task_action)
        task.save()
        # Link departure node to first action in the plan
        first_action_id = task.plan[0].actions[0].action_id
        timetable.update_action_id(task.task_id, "departure", first_action_id)
        timetable.store()

    def dispatch_tasks(self):
        for robot_id in self.robot_ids:
            timetable = self.timetable_manager.get_timetable(robot_id)
            task_id = timetable.get_earliest_task_id()
            task = self.tasks.get(task_id) if task_id else None
            if task and task.status.status == TaskStatusConst.ALLOCATED:
                start_time = timetable.get_departure_time(task.task_id)
                if self.is_schedulable(start_time):
                    self.add_pre_task_action(task, robot_id, timetable)
                    self.send_d_graph_update(robot_id)
                    self.dispatch_task(task, robot_id)

    def dispatch_task(self, task, robot_id):
        """
        Sends a task to the appropriate robot in the fleet

        Args:
            task: a ropod.structs.task.Task object
            robot_id: a robot UUID
        """
        self.logger.debug("Dispatching task %s to robot %s", task.task_id, robot_id)
        task_msg = self.api.create_message(task)
        self.api.publish(task_msg, groups=['TASK-ALLOCATION'])
        task.update_status(TaskStatusConst.DISPATCHED)

    def send_d_graph_update(self, robot_id):
        timetable = self.timetable_manager.get_timetable(robot_id)
        prev_d_graph_update = self.d_graph_updates.get(robot_id)
        d_graph_update = timetable.get_d_graph_update(robot_id, self.n_queued_tasks)

        if prev_d_graph_update != d_graph_update:
            self.logger.debug("Sending DGraphUpdate to %s", robot_id)
            msg = self.api.create_message(d_graph_update)
            self.api.publish(msg, peer=str(robot_id))
            self.d_graph_updates[robot_id] = copy.deepcopy(d_graph_update)
