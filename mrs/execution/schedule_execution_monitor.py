import logging
from datetime import datetime

from fmlib.models import tasks
from ropod.structs.status import TaskStatus as TaskStatusConst

from mrs.exceptions.execution import InconsistentAssignment
from mrs.exceptions.execution import InconsistentSchedule
from mrs.messages.d_graph_update import DGraphUpdate
from mrs.messages.task_status import TaskStatus
from mrs.timetable.monitor import TimetableMonitorBase


class ScheduleExecutionMonitor(TimetableMonitorBase):

    def __init__(self, robot_id, timetable, scheduler, delay_recovery, **kwargs):
        """ Includes methods to monitor the schedule of a robot's allocated tasks
        """
        super().__init__(timetable=timetable, **kwargs)
        self.robot_id = robot_id
        self.timetable = timetable
        self.scheduler = scheduler
        self.recovery_method = delay_recovery
        self.d_graph_watchdog = kwargs.get("d_graph_watchdog", False)
        self.api = kwargs.get("api")

        self.d_graph_update_received = False
        self.tasks = dict()
        self.tasks_status = dict()
        self.task = None

        self.logger = logging.getLogger('mrs.schedule.monitor.%s' % self.robot_id)
        self.logger.debug("ScheduleMonitor initialized %s", self.robot_id)

    def task_cb(self, msg):
        payload = msg['payload']
        assigned_robots = payload.get("assignedRobots")
        if self.robot_id in assigned_robots:
            task_type = payload.pop("_cls").split('.')[-1]
            task_cls = getattr(tasks, task_type)
            task = task_cls.from_payload(payload, save=False)
            self.tasks[task.task_id] = task
            self.tasks_status[task.task_id] = TaskStatusConst.DISPATCHED
            self.logger.debug("Received task %s", task.task_id)

    def d_graph_update_cb(self, msg):
        payload = msg['payload']
        robot_id = payload.get('robotId')
        if robot_id == self.robot_id:
            self.logger.debug("Received DGraph update")
            d_graph_update = DGraphUpdate.from_payload(payload)
            d_graph_update.update_timetable(self.timetable)
            self.logger.debug("STN update %s", self.timetable.stn)
            self.logger.debug("Dispatchable graph update %s", self.timetable.dispatchable_graph)
            self.d_graph_update_received = True

    def process_task_status(self, task_status, timestamp):
        if self.robot_id == task_status.robot_id:
            self.logger.debug("Processing task status %s for task %s by %s", task_status.task_status,
                              task_status.task_id,
                              task_status.robot_id)
            self.send_task_status(task_status, timestamp)
            task = self.tasks.get(task_status.task_id)

            if task_status.task_status == TaskStatusConst.ONGOING:
                self.update_timetable(task, task_status.robot_id, task_status.task_progress, timestamp)
                self.tasks_status[task.task_id] = task_status.task_status

            if task_status.task_status == TaskStatusConst.COMPLETED:
                self.logger.debug("Completing execution of task %s", task.task_id)
                self.tasks.pop(task_status.task_id)
                self.tasks_status.pop(task_status.task_id)
                self.task = None
            else:
                self.tasks_status[task.task_id] = task_status.task_status

    def schedule(self, task):
        try:
            scheduled_task = self.scheduler.schedule(task)
            self.tasks[task.task_id] = scheduled_task
            self.tasks_status[task.task_id] = TaskStatusConst.SCHEDULED
        except InconsistentSchedule:
            if "re-allocate" in self.recovery_method:
                self.re_allocate(task)
            else:
                self.cancel(task)

    def _update_timepoint(self, task, timetable, r_assigned_time, node_id, task_progress, store=False):
        is_consistent = True
        try:
            self.timetable.assign_timepoint(r_assigned_time, node_id)
        except InconsistentAssignment as e:
            self.logger.warning("Assignment of time %s to task %s node_type %s is inconsistent "
                                "Assigning anyway.. ", e.assigned_time, e.task_id, e.node_type)
            self.timetable.stn.assign_timepoint(e.assigned_time, node_id, force=True)
            is_consistent = False

        self.timetable.stn.execute_timepoint(node_id)
        self._update_edges(task, timetable, store)

        if not self.d_graph_watchdog:
            self.recover(task, task_progress, r_assigned_time, is_consistent)

    def recover(self, task, task_progress, r_assigned_time, is_consistent):
        """ Applies a recovery method (preventive or corrective) if needed.
        A preventive recovery prevents delay of next_task. Applied BEFORE current task becomes inconsistent
        A corrective recovery prevents delay of next task. Applied AFTER current task becomes inconsistent

        task (Task) : current task
        is_consistent (boolean): True if the last assignment was consistent, false otherwise
        """

        task_to_recover = self.recovery_method.recover(self.timetable, task, task_progress, r_assigned_time, is_consistent)

        if task_to_recover and self.recovery_method.name == "re-allocate":
            self.re_allocate(task_to_recover)

        elif task_to_recover and self.recovery_method.name == "cancel":
            self.cancel(task_to_recover)

    def re_allocate(self, task):
        self.logger.info("Trigger re-allocation of task %s", task.task_id)
        self.tasks_status[task.task_id] = TaskStatusConst.UNALLOCATED
        self.timetable.remove_task(task.task_id)
        task_status = TaskStatus(task.task_id, self.robot_id, TaskStatusConst.UNALLOCATED)
        self.send_task_status(task_status)

    def cancel(self, task):
        self.logger.info("Trigger cancellation of task %s", task.task_id)
        self.tasks_status[task.task_id] = TaskStatusConst.CANCELED
        self.timetable.remove_task(task.task_id)
        task_status = TaskStatus(task.task_id, self.robot_id, TaskStatusConst.CANCELED)
        self.send_task_status(task_status)

    def send_task_status(self, task_status, timestamp=None):
        self.logger.debug("Sending task status for task %s", task_status.task_id)
        msg = self.api.create_message(task_status)
        if timestamp:
            msg["header"]["timestamp"] = timestamp.isoformat()
        self.api.publish(msg, groups=["TASK-ALLOCATION"])

    def send_task(self, task):
        self.logger.debug("Sending task %s to executor", task.task_id)
        task_msg = self.api.create_message(task)
        self.api.publish(task_msg, peer='executor_' + self.robot_id)

    def run(self):
        """ Gets the earliest task assigned to this robot and calls the ``process_task`` method
        for further processing
        """
        if self.tasks and self.task is None:
            tasks = list(self.tasks.values())
            earliest_task = self.get_earliest_task(tasks)
            self.process_task(earliest_task)

    @staticmethod
    def get_earliest_task(tasks):
        earliest_time = datetime.max
        earliest_task = None
        for task in tasks:
            if task.start_constraint.earliest_time < earliest_time:
                earliest_time = task.start_constraint.earliest_time
                earliest_task = task
        return earliest_task

    def process_task(self, task):
        task_status = self.tasks_status.get(task.task_id)
        print("Process task: ", task.task_id)
        print("Task status: ", task_status)

        if task_status == TaskStatusConst.DISPATCHED and self.timetable.has_task(task.task_id):
            print("Schedule task")
            self.schedule(task)

        # For real-time execution add is_executable condition
        if task_status == TaskStatusConst.SCHEDULED:
            self.send_task(task)
            self.task = task
