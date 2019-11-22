
import logging

from ropod.structs.task import TaskStatus as TaskStatusConst

from mrs.dispatching.schedule_monitor import ScheduleMonitor
from mrs.dispatching.d_graph_update import DGraphUpdate
from mrs.db.models.task import TaskLot


class Dispatcher(object):

    def __init__(self, stp_solver, timetable_manager, freeze_window, **kwargs):
        """ Dispatches tasks to a multi-robot system based on temporal constraints

        Args:

            stp_solver (STP): Simple Temporal Problem object
            timetable_manager (TimetableManager): contains the timetables of all the robots in the fleet
            freeze_window (float): Defines the time (minutes) within which a task can be scheduled
                        e.g, with a freeze window of 2 minutes, a task can be scheduled if its earliest
                        start navigation time is within the next 2 minutes.
            kwargs:
                api (API): object that provides middleware functionality
                robot_store (robot_store): interface to interact with the db
        """
        self.logger = logging.getLogger('mrs.dispatcher')
        self.api = kwargs.get('api')
        self.ccu_store = kwargs.get('ccu_store')

        self.stp_solver = stp_solver
        self.timetable_manager = timetable_manager
        self.schedule_monitor = ScheduleMonitor(freeze_window)

        self.robot_ids = list()

        self.logger.debug("Dispatcher started")

    def configure(self, **kwargs):
        api = kwargs.get('api')
        ccu_store = kwargs.get('ccu_store')
        if api:
            self.api = api
        if ccu_store:
            self.ccu_store = ccu_store

    def register_robot(self, robot_id):
        self.logger.debug("Registering robot %s", robot_id)
        self.robot_ids.append(robot_id)

    def run(self):
        if self.timetable_manager.send_update_to:
            robot_id = self.timetable_manager.send_update_to
            self.timetable_manager.send_update_to = None
            timetable = self.timetable_manager.get_timetable(robot_id)
            self.send_d_graph_update(timetable, robot_id)

        self.dispatch_tasks()

    def dispatch_tasks(self):
        for robot_id in self.robot_ids:
            timetable = self.timetable_manager.get_timetable(robot_id)
            tasks = timetable.get_earliest_tasks()
            for task in tasks:
                if task.status.status == TaskStatusConst.PLANNED:
                    start_time = timetable.get_start_time(task.task_id)
                    if self.schedule_monitor.is_schedulable(start_time):
                        TaskLot.freeze_task(task.task_id)
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
        self.api.publish(task_msg)
        task.update_status(TaskStatusConst.DISPATCHED)

    def send_d_graph_update(self, timetable, robot_id):
        self.logger.debug("Sending DGraphUpdate to %s", robot_id)
        sub_dispatchable_graph = timetable.dispatchable_graph.get_subgraph(n_tasks=self.timetable_manager.n_tasks_queue)
        d_graph_update = DGraphUpdate(self.timetable_manager.zero_timepoint, sub_dispatchable_graph)
        d_graph_update_msg = self.api.create_message(d_graph_update)
        self.api.publish(d_graph_update_msg, peer=robot_id)




