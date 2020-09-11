import logging
import time

from fmlib.models.robot import Robot
from fmlib.models.tasks import TransportationTask as Task
from pymodm.errors import DoesNotExist
from ropod.structs.status import TaskStatus as TaskStatusConst, ActionStatus as ActionStatusConst
from ropod.utils.timestamp import TimeStamp
from stn.exceptions.stp import NoSTPSolution

from mrs.exceptions.allocation import TaskNotFound
from mrs.exceptions.execution import EmptyTimetable
from mrs.messages.remove_task import RemoveTaskFromSchedule
from mrs.messages.task_status import TaskStatus, TaskProgress
from mrs.simulation.simulator import SimulatorInterface
from mrs.utils.time import relative_to_ztp


class TimetableMonitorBase:
    def __init__(self, **kwargs):
        self.task_manager = kwargs.get("timetable_manager")
        self.timetable = kwargs.get("timetable")
        self.d_graph_watchdog = kwargs.get("d_graph_watchdog", False)
        self.api = kwargs.get('api')
        self.logger = logging.getLogger("mrs.timetable.monitor")
        self.tasks = dict()

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            self.logger.debug("Adding %s", key)
            self.__dict__[key] = value

    def get_timetable(self, robot_id):
        if self.task_manager:
            return self.task_manager.get_timetable(robot_id)
        elif self.timetable:
            return self.timetable

    def task_status_cb(self, msg):
        payload = msg['payload']
        timestamp = TimeStamp.from_str(msg["header"]["timestamp"]).to_datetime()
        task_status = TaskStatus.from_payload(payload)
        self.process_task_status(task_status, timestamp)

    def process_task_status(self, task_status, timestamp):
        try:
            task = self.tasks.get(task_status.task_id)
            if task_status.task_status == TaskStatusConst.ONGOING:
                self.process_task_status_update(task, task_status, timestamp)
                self.tasks_status[task.task_id] = task_status.task_status
            if task_status.task_status == TaskStatusConst.COMPLETED:
                self.process_task_status_update(task, task_status, timestamp)
        except DoesNotExist:
            self.logger.warning("Task %s does not exist", task_status.task_id)

    def process_task_status_update(self, task, task_status, timestamp):
        self.update_timetable(task, task_status.robot_id, task_status.task_progress, timestamp)

    def update_timetable(self, task, robot_id, task_progress, timestamp):
        if isinstance(task_progress, dict):
            task_progress = TaskProgress.from_dict(task_progress)

        self.logger.debug("Task progress: %s", task_progress)
        timetable = self.get_timetable(robot_id)
        r_assigned_time = relative_to_ztp(timetable.ztp, timestamp)
        nodes = timetable.stn.get_nodes_by_action(task_progress.action_id)
        action_status = task_progress.action_status.status

        for node_id, node in nodes:
            if not node.is_executed and \
                    ((node.node_type in ["departure", "start"] and action_status == ActionStatusConst.ONGOING) or
                     (node.node_type == "finish" and action_status == ActionStatusConst.COMPLETED)):
                self.logger.debug("Updating timepoint: %s", node.node_type)
                self._update_timepoint(task, timetable, r_assigned_time, node_id, task_progress)

    def _update_timepoint(self, task, timetable, r_assigned_time, node_id, task_progress, store=True):
        timetable.update_timepoint(r_assigned_time, node_id)
        self._update_edges(task, timetable, store)

    def _update_edges(self, task, timetable, store=True):
        nodes = timetable.stn.get_nodes_by_task(task.task_id)
        self._update_edge(timetable, 'departure', 'start', nodes)
        self._update_edge(timetable, 'start', 'finish', nodes)

        self.logger.debug("Updated stn: \n %s ", timetable.stn)
        self.logger.debug("Updated dispatchable graph: \n %s", timetable.dispatchable_graph)
        if store:
            timetable.store()

    @staticmethod
    def _update_edge(timetable, start_node, finish_node, nodes):
        node_ids = [node_id for node_id, node in nodes if (node.node_type == start_node and node.is_executed) or
                    (node.node_type == finish_node and node.is_executed)]
        if len(node_ids) == 2:
            timetable.execute_edge(node_ids[0], node_ids[1])

    def _update_progress(self, task, task_progress, timestamp):
        self.logger.debug("Updating progress of task %s action %s status %s", task.task_id, task_progress.action_id,
                          task_progress.action_status.status)
        if not task.status.progress:
            task.update_progress(task_progress.action_id, task_progress.action_status.status)
        action_progress = task.status.progress.get_action(task_progress.action_id)

        self.logger.debug("Current action progress: status %s, start time %s, finish time %s", action_progress.status,
                          action_progress.start_time, action_progress.finish_time)
        kwargs = {}
        if task_progress.action_status.status == ActionStatusConst.ONGOING:
            kwargs.update(start_time=timestamp)
        elif task_progress.action_status.status == ActionStatusConst.COMPLETED:
            kwargs.update(start_time=action_progress.start_time, finish_time=timestamp)

        task.update_progress(task_progress.action_id, task_progress.action_status.status, **kwargs)
        action_progress = task.status.progress.get_action(task_progress.action_id)

        self.logger.debug("Updated action progress: status %s, start time %s, finish time %s", action_progress.status,
                          action_progress.start_time, action_progress.finish_time)

    def _update_task_schedule(self, task, task_progress, timestamp):
        # TODO: Get schedule from dispatchable graph
        first_action_id = task.plan[0].actions[0].action_id
        last_action_id = task.plan[0].actions[-1].action_id

        if task_progress.action_id == first_action_id and \
                task_progress.action_status.status == ActionStatusConst.ONGOING:
            self.logger.debug("Task %s departure time %s", task.task_id, timestamp)
            task_schedule = {"departure_time": timestamp,
                             "finish_time": task.finish_time}
            task.update_schedule(task_schedule)

        elif task_progress.action_id == last_action_id and \
                task_progress.action_status.status == ActionStatusConst.COMPLETED:
            self.logger.debug("Task %s finish time %s", task.task_id, timestamp)
            task_schedule = {"start_time": task.start_time,
                             "finish_time": timestamp}
            task.update_schedule(task_schedule)

    def _re_compute_dispatchable_graph(self, timetable, next_task=None):
        """ Recomputes the timetable's dispatchable graph.

        Args:
            timetable (obj): Timetable containing an stn and a dispatchable graph

        """
        if timetable.stn.is_empty():
            self.logger.warning("Timetable of %s is empty", timetable.robot_id)
            raise EmptyTimetable
        self.logger.debug("Recomputing dispatchable graph of robot %s", timetable.robot_id)
        try:
            timetable.dispatchable_graph = timetable.compute_dispatchable_graph(timetable.stn)
            self.logger.debug("Dispatchable graph robot %s: %s", timetable.robot_id, timetable.dispatchable_graph)
            return True
        except NoSTPSolution:
            self.logger.warning("Temporal network is inconsistent")
            return False

    def remove_task_from_timetable(self, timetable, task, status, next_task=None, store=True):
        self.logger.debug("Deleting task %s from timetable ", task.task_id)

        if not timetable.has_task(task.task_id):
            self.logger.warning("Robot %s does not have task %s in its timetable: ", timetable.robot_id, task.task_id)
            raise TaskNotFound

        if timetable.stn.is_empty():
            self.logger.warning("Timetable of %s is empty", timetable.robot_id)
            raise EmptyTimetable

        prev_task_id = timetable.get_previous_task_id(task)
        prev_task = self.tasks.get(prev_task_id)
        earliest_task_id = timetable.get_task_id(position=1)

        if task.task_id == earliest_task_id and next_task:
            self._remove_first_task(task, next_task, status, timetable)
        else:
            timetable.remove_task(task.task_id)

        if prev_task and next_task:
            self.update_pre_task_constraint(prev_task, next_task, timetable)

        self.tasks.pop(task.task_id)

        if store:
            timetable.store()
        self.logger.debug("STN robot %s: %s", timetable.robot_id, timetable.stn)
        self.logger.debug("Dispatchable graph robot %s: %s", timetable.robot_id, timetable.dispatchable_graph)

    @staticmethod
    def _remove_first_task(task, next_task, status, timetable):
        if status == TaskStatusConst.COMPLETED:
            earliest_time = timetable.stn.get_time(task.task_id, 'finish', False)
            timetable.stn.assign_earliest_time(earliest_time, next_task.task_id, 'departure', force=True)
        else:
            nodes = timetable.stn.get_nodes_by_task(task.task_id)
            node_id, node = nodes[0]
            earliest_time = timetable.stn.get_node_earliest_time(node_id)
            timetable.stn.assign_earliest_time(earliest_time, next_task.task_id, 'departure', force=True)

        start_next_task = timetable.dispatchable_graph.get_time(next_task.task_id, 'departure')
        if start_next_task < earliest_time:
            timetable.dispatchable_graph.assign_earliest_time(earliest_time, next_task.task_id, 'departure', force=True)

        timetable.remove_task(task.task_id)

    def update_pre_task_constraint(self, prev_task, task, timetable):
        self.logger.debug("Update pre_task constraint of task %s", task.task_id)
        prev_location = prev_task.request.finish_location
        path = self.planner.get_path(prev_location, task.request.start_location)
        mean, variance = self.planner.get_estimated_duration(path)

        stn_task = timetable.get_stn_task(task.task_id)
        stn_task.update_edge("travel_time", mean, variance)
        timetable.add_stn_task(stn_task)
        timetable.update_task(stn_task)

    def update_robot_poses(self, task):
        for robot_id in task.assigned_robots:
            robot = Robot.get_robot(robot_id)
            x, y, theta = self.planner.get_pose(task.request.finish_location)
            robot.update_position(x=x, y=y, theta=theta)


class TimetableMonitor(TimetableMonitorBase):
    def __init__(self, auctioneer, delay_recovery, **kwargs):
        super().__init__(**kwargs)

        self.auctioneer = auctioneer
        self.recovery_method = delay_recovery
        self.d_graph_watchdog = kwargs.get("d_graph_watchdog", False)
        self.api = kwargs.get('api')
        self.simulator_interface = SimulatorInterface(kwargs.get('simulator'))

        self.tasks_to_remove = list()
        self.tasks_to_reallocate = list()
        self.completed_tasks = list()
        self.deleting_task = False
        self.processing_task = False
        self.logger.debug("Timetable monitor started")

    def task_status_cb(self, msg):
        while self.deleting_task:
            time.sleep(0.1)
        self.processing_task = True
        super().task_status_cb(msg)
        self.processing_task = False

    def process_task_status(self, task_status, timestamp):
        self.logger.debug("Processing task status %s for task %s by robot %s", task_status.task_status, task_status.task_id,
                          task_status.robot_id)
        try:
            task = Task.get_task(task_status.task_id)
            if task_status.task_status == TaskStatusConst.ONGOING:
                self.process_task_status_update(task, task_status, timestamp)
                task.update_status(task_status.task_status)
            if task_status.task_status == TaskStatusConst.COMPLETED:
                self.process_task_status_update(task, task_status, timestamp)
                self.logger.debug("Adding task %s to tasks to remove", task.task_id)
                self.tasks_to_remove.append((task, task_status.task_status))

            elif task_status.task_status == TaskStatusConst.UNALLOCATED:
                self.re_allocate(task)

            elif task_status.task_status == TaskStatusConst.PREEMPTED:
                self.preempt(task)
        except DoesNotExist:
            self.logger.warning("Task %s does not exist", task_status.task_id)

    def process_task_status_update(self, task, task_status, timestamp):
        super().process_task_status_update(task, task_status, timestamp)
        self._update_progress(task, task_status.task_progress, timestamp)
        self._update_task_schedule(task, task_status.task_progress, timestamp)

    def _update_timepoint(self, task, timetable, r_assigned_time, node_id, task_progress, store=True):
        timetable.check_is_task_delayed(task, r_assigned_time, node_id)
        try:
            self.performance_tracker.update_delay(task.task_id, r_assigned_time, node_id, timetable)
            self.performance_tracker.update_earliness(task.task_id, r_assigned_time, node_id, timetable)
        except AttributeError:
            pass
        super()._update_timepoint(task, timetable, r_assigned_time, node_id, task_progress, store)

        try:
            self.performance_tracker.update_timetables(timetable)
        except AttributeError:
            pass
        self.auctioneer.changed_timetable.append(timetable.robot_id)

        if self.d_graph_watchdog:
            next_task_id = timetable.get_next_task_id(task)
            next_task = Task.get_task(next_task_id)
            self._re_compute_dispatchable_graph(timetable, next_task)

    def _re_compute_dispatchable_graph(self, timetable, next_task=None):
        try:
            successful_recomputation = super()._re_compute_dispatchable_graph(timetable)
            if successful_recomputation:
                self.auctioneer.changed_timetable.append(timetable.robot_id)
            elif not successful_recomputation and next_task:
                self.recover(next_task)
        except EmptyTimetable:
            pass

    def recover(self, task):
        if self.recovery_method.name == "preempt":
            self.preempt(task)
        elif self.recovery_method.name == "re-allocate":
            self.re_allocate(task)

    def remove_task(self, task, status):
        for robot_id in task.assigned_robots:
            try:
                timetable = self.get_timetable(robot_id)
                next_task_id = timetable.get_next_task_id(task)
                if next_task_id:
                    next_task = Task.get_task(next_task_id)
                else:
                    next_task = None
                self.remove_task_from_timetable(timetable, task, status, next_task)
                # TODO: The robot should send a ropod-pose msg and the fleet monitor should update the robot pose
                if status == TaskStatusConst.COMPLETED:
                    self.update_robot_poses(task)
                task.update_status(status)
                self.auctioneer.changed_timetable.append(timetable.robot_id)
                self.send_remove_task(task.task_id, status, robot_id)
                self._re_compute_dispatchable_graph(timetable, next_task)
            except (TaskNotFound, EmptyTimetable):
                return

    def re_allocate(self, task):
        self.logger.info("Re-allocating task %s", task.task_id)
        self.remove_task(task, TaskStatusConst.UNALLOCATED)
        task.unassign_robots()
        self.tasks[task.task_id] = task
        self.auctioneer.allocated_tasks.pop(task.task_id)
        self.auctioneer.allocate(task)
        self.tasks_to_reallocate.append(task)

    def preempt(self, task):
        self.logger.info("Preempting task %s", task.task_id)
        self.remove_task(task, TaskStatusConst.PREEMPTED)

    def send_remove_task(self, task_id, status, robot_id):
        remove_task = RemoveTaskFromSchedule(task_id, status)
        msg = self.api.create_message(remove_task)
        self.api.publish(msg, peer=str(robot_id) + '_proxy')

    def run(self):
        # TODO: Check how this works outside simulation
        ready_to_be_removed = list()
        if not self.processing_task:

            for task, status in self.tasks_to_remove:
                if task.finish_time < self.simulator_interface.get_current_time():
                    self.deleting_task = True
                    ready_to_be_removed.append((task, status))

            for task, status in ready_to_be_removed:
                self.tasks_to_remove.remove((task, status))
                if status == TaskStatusConst.COMPLETED:
                    self.completed_tasks.append(task)
                self.remove_task(task, status)

        if self.deleting_task and not self.completed_tasks:
            self.deleting_task = False


class TimetableMonitorProxy(TimetableMonitorBase):
    def __init__(self, robot_id, bidder, **kwargs):
        super().__init__(**kwargs)
        self.robot_id = robot_id
        self.bidder = bidder
        self.logger = logging.getLogger("mrs.proxy.timetable.monitor")
        self.logger.debug("Timetable monitor started")

    def process_task_status(self, task_status, timestamp):
        if self.robot_id == task_status.robot_id:
            self.logger.debug("Processing task status %s for task %s by robot %s", task_status.task_status,
                              task_status.task_id,
                              task_status.robot_id)
            super().process_task_status(task_status, timestamp)

    def remove_task_cb(self, msg):
        payload = msg['payload']
        remove_task = RemoveTaskFromSchedule.from_payload(payload)
        task = self.tasks.get(remove_task.task_id)
        self.remove_task(task, remove_task.status)

    def remove_task(self, task, status):
        try:
            timetable = self.get_timetable(self.robot_id)
            next_task_id = timetable.get_next_task_id(task)
            next_task = self.tasks.get(next_task_id, None)
            self.remove_task_from_timetable(timetable, task, status, next_task, store=False)
            # TODO: The robot should send a ropod-pose msg and the robot proxy should update the robot pose
            if status == TaskStatusConst.COMPLETED:
                self.update_robot_pose(task)
            self.tasks_status[task.task_id] = status
            self.bidder.changed_timetable = True
            self._re_compute_dispatchable_graph(timetable, next_task)
        except (TaskNotFound, EmptyTimetable):
            return

    def update_robot_pose(self, task):
        x, y, theta = self.planner.get_pose(task.request.finish_location)
        self.robot.update_position(save=False, x=x, y=y, theta=theta)

    def _update_timepoint(self, task, timetable, r_assigned_time, node_id, task_progress, store=False):
        super()._update_timepoint(task, timetable, r_assigned_time, node_id, task_progress, store)
        self.bidder.changed_timetable = True

        if self.d_graph_watchdog:
            self._re_compute_dispatchable_graph(timetable)

    def _re_compute_dispatchable_graph(self, timetable, next_task=None):
        try:
            successful_recomputation = super()._re_compute_dispatchable_graph(timetable)
            if successful_recomputation:
                self.bidder.changed_timetable = True
        except EmptyTimetable:
            pass
