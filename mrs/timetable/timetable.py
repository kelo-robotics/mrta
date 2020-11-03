import copy
import logging
from datetime import timedelta

from fmlib.models.tasks import Task
from fmlib.models.tasks import TaskStatus
from fmlib.models.tasks import TimepointConstraint
from fmlib.models.timetable import Timetable as TimetableMongo
from mrs.exceptions.allocation import InvalidAllocation
from mrs.exceptions.allocation import TaskNotFound
from mrs.exceptions.execution import InconsistentAssignment
from mrs.messages.d_graph_update import DGraphUpdate
from mrs.simulation.simulator import SimulatorInterface
from mrs.timetable.stn_interface import STNInterface
from mrs.utils.time import to_timestamp
from pymodm.context_managers import switch_collection
from pymodm.errors import DoesNotExist
from ropod.utils.timestamp import TimeStamp
from stn.exceptions.stp import NoSTPSolution
from stn.methods.fpc import get_minimal_network
from stn.stp import STP
from stn.task import Task as STNTask


class Timetable(STNInterface):
    """
    Each robot has a timetable, which contains temporal information about the robot's
    allocated tasks:
    - stn (stn):    Simple Temporal Network.
                    Contains the allocated tasks along with the original temporal constraints

    - dispatchable graph (stn): Uses the same data structure as the stn and contains the same tasks, but
                            shrinks the original temporal constraints to the times at which the robot
                            can allocate the task

    """

    def __init__(self, robot_id, stp_solver, **kwargs):

        self.robot_id = robot_id
        self.stp_solver = stp_solver

        simulator_interface = SimulatorInterface(kwargs.get("simulator"))

        self.ztp = simulator_interface.init_ztp()
        self.stn = self.stp_solver.get_stn()
        self.dispatchable_graph = self.stp_solver.get_stn()
        super().__init__(self.ztp, self.stn, self.dispatchable_graph)

        self.logger = logging.getLogger("mrs.timetable.%s" % self.robot_id)
        self.logger.debug("Timetable %s started", self.robot_id)

    def update_ztp(self, time_):
        self.ztp.timestamp = time_
        self.logger.debug("Zero timepoint updated to: %s", self.ztp)

    def compute_dispatchable_graph(self, stn):
        try:
            dispatchable_graph = self.stp_solver.solve(stn)
            return dispatchable_graph
        except NoSTPSolution:
            raise NoSTPSolution()

    def assign_timepoint(self, assigned_time, node_id):
        stn = copy.deepcopy(self.stn)
        minimal_network = get_minimal_network(stn)
        if minimal_network:
            minimal_network.assign_timepoint(assigned_time, node_id, force=True)
            if self.stp_solver.is_consistent(minimal_network):
                self.stn.assign_timepoint(assigned_time, node_id, force=True)
                return
        node = self.stn.get_node(node_id)
        raise InconsistentAssignment(assigned_time, node.task_id, node.node_type)

    def is_next_task_invalid(self, task, next_task):
        finish_current_task = self.dispatchable_graph.get_time(task.task_id, 'finish', False)
        earliest_departure_next_task = self.dispatchable_graph.get_time(next_task.task_id, 'departure')
        latest_departure_next_task = self.dispatchable_graph.get_time(next_task.task_id, 'departure', False)
        if latest_departure_next_task < finish_current_task:
            self.logger.warning("Task %s is invalid", next_task.task_id)
            return True
        elif earliest_departure_next_task < finish_current_task:
            # Next task is valid but we need to update its earliest departure time
            self.dispatchable_graph.assign_earliest_time(finish_current_task, next_task.task_id, "departure", force=True)
        return False

    def update_timepoint(self, assigned_time, node_id):
        self.stn.assign_timepoint(assigned_time, node_id, force=True)
        self.stn.execute_timepoint(node_id)
        self.dispatchable_graph.assign_timepoint(assigned_time, node_id, force=True)
        self.dispatchable_graph.execute_timepoint(node_id)

    def execute_edge(self, start_node_id, finish_node_id, archived_dispatchable_graph=None):
        self.stn.execute_edge(start_node_id, finish_node_id)
        self.stn.remove_old_timepoints()
        self.dispatchable_graph.execute_edge(start_node_id, finish_node_id)
        archived_dispatchable_graph = self.dispatchable_graph.remove_old_timepoints(archived_dispatchable_graph)
        return archived_dispatchable_graph

    def update_action_id(self, task_id, node_type, action_id):
        node_id, node = self.stn.get_node_by_type(task_id, node_type)
        node.action_id = action_id

    def get_tasks(self):
        """ Returns the tasks contained in the timetable

        :return: list of tasks
        """
        return self.stn.get_tasks()

    def get_task_id(self, position):
        """ Returns the task_id in the given position

        :param position: (int) position in the STN
        :return: (uuid.UUID) task_id
        """
        task_id = self.stn.get_task_id(position)
        if task_id:
            return task_id
        else:
            raise TaskNotFound(position)

    def get_task_node_ids(self, task_id):
        return self.stn.get_task_node_ids(task_id)

    def get_next_task_id(self, task):
        task_last_node = self.stn.get_task_node_ids(task.task_id)[-1]
        if self.stn.has_node(task_last_node + 1):
            next_task_id = self.stn.nodes[task_last_node + 1]['data'].task_id
            return next_task_id

    def get_previous_task_id(self, task):
        task_first_node = self.stn.get_task_node_ids(task.task_id)[0]
        if task_first_node > 1 and self.stn.has_node(task_first_node - 1):
            prev_task_id = self.stn.nodes[task_first_node - 1]['data'].task_id
            return prev_task_id

    def get_task_position(self, task_id):
        return self.stn.get_task_position(task_id)

    def has_task(self, task_id):
        task_nodes = self.stn.get_task_node_ids(task_id)
        if task_nodes:
            return True
        return False

    def get_earliest_task_id(self):
        return self.stn.get_earliest_task_id()

    def get_r_time(self, task_id, node_type, lower_bound):
        r_time = self.dispatchable_graph.get_time(task_id, node_type, lower_bound)
        return r_time

    def get_departure_time(self, task_id, lower_bound=True):
        r_departure_time = self.get_r_time(task_id, 'departure', lower_bound)
        departure_time = self.ztp + timedelta(seconds=r_departure_time)
        return departure_time

    def get_start_time(self, task_id, lower_bound=True):
        r_start_time = self.get_r_time(task_id, 'start', lower_bound)
        start_time = self.ztp + timedelta(seconds=r_start_time)
        return start_time

    def get_finish_time(self, task_id, lower_bound=True):
        r_finish_time = self.get_r_time(task_id, 'finish', lower_bound)
        finish_time = self.ztp + timedelta(seconds=r_finish_time)
        return finish_time

    def get_insertion_points(self, r_earliest_time):
        return self.stn.get_insertion_points(r_earliest_time)

    def check_is_task_delayed(self, task, assigned_time, node_id):
        latest_time = self.dispatchable_graph.get_node_latest_time(node_id)
        if assigned_time > latest_time:
            self.logger.warning("Task %s is delayed", task.task_id)
            task.delayed = True

    def remove_task(self, task_id, archived_timetable=None):
        stn_task = self.stn_tasks.pop(str(task_id))
        self.remove_task_from_stn(task_id)
        if archived_timetable:
            archived_timetable.dispatchable_graph = self.remove_task_from_dispatchable_graph(task_id,
                                                                                             archived_timetable.dispatchable_graph)
            archived_timetable.add_stn_task(stn_task)
        else:
            self.remove_task_from_dispatchable_graph(task_id)
        return archived_timetable

    def remove_task_from_stn(self, task_id):
        task_node_ids = self.stn.get_task_node_ids(task_id)
        if 0 < len(task_node_ids) < 3:
            self.stn.remove_node_ids(task_node_ids)
            self.stn.displace_nodes(0)
        elif len(task_node_ids) == 3:
            node_id = self.stn.get_task_position(task_id)
            self.stn.remove_task(node_id)
        else:
            self.logger.warning("Task %s is not in timetable", task_id)

    def remove_task_from_dispatchable_graph(self, task_id, archived_dispatchable_graph=None):
        task_node_ids = self.dispatchable_graph.get_task_node_ids(task_id)
        if 0 < len(task_node_ids) < 3:
            archived_dispatchable_graph = self.dispatchable_graph.remove_node_ids(task_node_ids, archived_dispatchable_graph)
            self.dispatchable_graph.displace_nodes(0)
            return archived_dispatchable_graph
        elif len(task_node_ids) == 3:
            node_id = self.dispatchable_graph.get_task_position(task_id)
            self.dispatchable_graph.remove_task(node_id)
        else:
            self.logger.warning("Task %s is not in timetable", task_id)

    def remove_node_ids(self, task_node_ids):
        self.stn.remove_node_ids(task_node_ids)
        self.dispatchable_graph.remove_node_ids(task_node_ids)

    def get_timepoint_constraint(self, task_id, constraint_name):
        earliest_time = to_timestamp(self.ztp,
                                     self.get_r_time(task_id, constraint_name, lower_bound=True)).to_datetime()
        latest_time = to_timestamp(self.ztp,
                                   self.get_r_time(task_id, constraint_name, lower_bound=False)).to_datetime()
        constraint = TimepointConstraint(earliest_time, latest_time)
        return constraint

    def get_d_graph_update(self, robot_id, n_tasks):
        sub_stn = self.stn.get_subgraph(n_tasks)
        sub_dispatchable_graph = self.dispatchable_graph.get_subgraph(n_tasks)
        return DGraphUpdate(robot_id, self.ztp, sub_stn, sub_dispatchable_graph)

    def get_tasks_for_timetable_update(self, other, **kwargs):
        tasks = list()
        task_ids = kwargs.get("task_ids", list())
        status = kwargs.get("status", TaskStatus.in_timetable)
        earliest_time = kwargs.get("earliest_time")
        latest_time = kwargs.get("latest_time")

        for i in sorted(self.dispatchable_graph.nodes()):
            if 'data' in self.dispatchable_graph.nodes[i]:
                node_data = self.dispatchable_graph.nodes[i]['data']
                if node_data.task_id not in task_ids \
                        and node_data.node_type != 'zero_timepoint':
                    try:
                        task = Task.get_task(node_data.task_id)
                    except DoesNotExist:
                        task = Task.get_archived_task(node_data.task_id)

                    # Do not include tasks whose status are not in the given list of status
                    if task.status.status not in status:
                        continue

                    task_dict = {"task_id": str(task.task_id),
                                 "type": task.type,
                                 "status": task.status.status,
                                 }

                    start_times = self.dispatchable_graph.get_times(task.task_id, "start")

                    # Only include tasks whose start constraints are within the given [earliest_time, latest_time]
                    if not start_times:
                        start_times = other.dispatchable_graph.get_times(task.task_id, "start")
                    start = self.get_timepoint_dict(start_times)
                    if earliest_time and latest_time and not self.time_is_within_tw(start, earliest_time, latest_time):
                        continue
                    task_dict.update(start=start)

                    departure_times = self.dispatchable_graph.get_times(task.task_id, "departure")
                    finish_times = self.dispatchable_graph.get_times(task.task_id, "finish")

                    if not departure_times:
                        departure_times = other.dispatchable_graph.get_times(task.task_id, "departure")
                    departure = self.get_timepoint_dict(departure_times)
                    task_dict.update(departure=departure)

                    if not finish_times:
                        finish_times = other.dispatchable_graph.get_times(task.task_id, "finish")
                    finish = self.get_timepoint_dict(finish_times)
                    task_dict.update(finish=finish)

                    tasks.append(task_dict)
                    task_ids.append(task.task_id)

        return tasks, task_ids

    def get_timepoint_dict(self, times_):
        return {"earliest": TimeStamp.to_str(to_timestamp(self.ztp, times_[0])),
                "latest": TimeStamp.to_str(to_timestamp(self.ztp, times_[1]))
                }

    @staticmethod
    def time_is_within_tw(timepoint_dict, earliest_time, latest_time):
        if TimeStamp.from_str(timepoint_dict["earliest"]) >= earliest_time and\
                TimeStamp.from_str(timepoint_dict["latest"]) <= latest_time:
            return True
        return False

    def to_dict(self):
        timetable_dict = dict()
        timetable_dict['robot_id'] = self.robot_id
        timetable_dict['solver_name'] = self.stp_solver.solver_name
        timetable_dict['ztp'] = self.ztp.to_str()
        timetable_dict['stn'] = self.stn.to_dict()
        timetable_dict['dispatchable_graph'] = self.dispatchable_graph.to_dict()
        timetable_dict['stn_tasks'] = self.stn_tasks

        return timetable_dict

    @staticmethod
    def from_dict(timetable_dict):
        robot_id = timetable_dict['robot_id']
        stp_solver = STP(timetable_dict['solver_name'])
        timetable = Timetable(robot_id, stp_solver)
        stn_cls = timetable.stp_solver.get_stn()

        ztp = timetable_dict.get('ztp')
        timetable.ztp = TimeStamp.from_str(ztp)
        timetable.stn = stn_cls.from_dict(timetable_dict['stn'])
        timetable.dispatchable_graph = stn_cls.from_dict(timetable_dict['dispatchable_graph'])
        timetable.stn_tasks = timetable_dict['stn_tasks']

        return timetable

    def to_model(self):
        stn_tasks = {task_id: task.to_dict() for (task_id, task) in self.stn_tasks.items()}

        timetable_model = TimetableMongo(self.robot_id,
                                         self.stp_solver.solver_name,
                                         self.ztp.to_datetime(),
                                         self.stn.to_dict(),
                                         self.dispatchable_graph.to_dict(),
                                         stn_tasks)
        return timetable_model

    def store(self):
        timetable = self.to_model()
        timetable.save()

    def archive(self):
        timetable = self.to_model()
        with switch_collection(TimetableMongo, TimetableMongo.Meta.archive_collection):
           timetable.save()

    def fetch(self):
        try:
            self.logger.debug("Fetching timetable of robot %s", self.robot_id)
            timetable_mongo = TimetableMongo.objects.get_timetable(self.robot_id)
            self.stn = self.stn.from_dict(timetable_mongo.stn)
            self.dispatchable_graph = self.stn.from_dict(timetable_mongo.dispatchable_graph)
            self.ztp = TimeStamp.from_datetime(timetable_mongo.ztp)
            self.stn_tasks = {task_id: STNTask.from_dict(task) for (task_id, task) in timetable_mongo.stn_tasks.items()}

        except DoesNotExist:
            self.logger.debug("The timetable of robot %s is empty", self.robot_id)
            # Resetting values
            self.stn = self.stp_solver.get_stn()
            self.dispatchable_graph = self.stp_solver.get_stn()
            # self.store()

    def fetch_archived(self):
        with switch_collection(TimetableMongo, TimetableMongo.Meta.archive_collection):
            self.fetch()


class TimetableManager:
    """
    Manages the timetable of all the robots in the fleet
    """
    def __init__(self, stp_solver, **kwargs):
        super().__init__()
        self.timetables = dict()
        self.archived_timetables = dict()
        self.logger = logging.getLogger("mrs.timetable.manager")
        self.stp_solver = stp_solver
        self.simulator = kwargs.get('simulator')

        self.logger.debug("TimetableManager started")

    @property
    def ztp(self):
        if self:
            any_timetable = next(iter(self.timetables.values()))
            return any_timetable.ztp
        else:
            self.logger.error("There are no robots registered.")

    @ztp.setter
    def ztp(self, time_):
        for robot_id, timetable in self.timetables.items():
            timetable.update_zero_timepoint(time_)

    def get_timetable(self, robot_id):
        return self.timetables.get(robot_id)

    def get_archived_timetable(self, robot_id):
        return self.archived_timetables.get(robot_id)

    def fetch_timetable(self, robot_id):
        timetable = Timetable(robot_id, self.stp_solver, simulator=self.simulator)
        timetable.fetch()
        self.timetables[robot_id] = timetable
        self.logger.debug("STN robot %s: %s", robot_id, timetable.stn)
        self.logger.debug("Dispatchable graph robot %s: %s", robot_id, timetable.dispatchable_graph)

    def fetch_archived_timetable(self, robot_id):
        timetable = Timetable(robot_id, self.stp_solver, simulator=self.simulator)
        timetable.fetch_archived()
        self.archived_timetables[robot_id] = timetable
        self.logger.debug("STN (archived) robot %s: %s", robot_id, timetable.stn)
        self.logger.debug("Dispatchable graph (archived) robot %s: %s", robot_id, timetable.dispatchable_graph)

    def register_robot(self, robot):
        self.logger.debug("Loading timetable of robot %s", robot.robot_id)
        self.fetch_timetable(robot.robot_id)
        self.fetch_archived_timetable(robot.robot_id)

    def unregister_robot(self, robot):
        self.timetables.pop(robot.robot_id)

    def fetch_timetables(self):
        for robot_id, timetable in self.timetables.items():
            timetable.fetch()

    def get_timetable_update_reply(self, robot_ids, status, earliest_time, latest_time):
        timetables = list()
        for robot_id in robot_ids:
            tasks = list()
            task_ids = list()
            timetable = self.get_timetable(robot_id)
            archived_timetable = self.get_archived_timetable(robot_id)

            if any(s in TaskStatus.archived_status for s in status):
                archived_tasks, task_ids = archived_timetable.get_tasks_for_timetable_update(timetable,
                                                                                             task_ids=task_ids,
                                                                                             status=status,
                                                                                             earliest_time=earliest_time,
                                                                                             latest_time=latest_time)
                tasks.extend(archived_tasks)

            if any(s in TaskStatus.in_timetable for s in status):
                not_archived_tasks, task_ids = timetable.get_tasks_for_timetable_update(archived_timetable,
                                                                                        task_ids=task_ids,
                                                                                        status=status,
                                                                                        earliest_time=earliest_time,
                                                                                        latest_time=latest_time)
                tasks.extend(not_archived_tasks)

            timetables.append({"robot_id": robot_id, "tasks": tasks})

        return timetables

    def update_timetable(self, robot_id, allocation_info, task):
        timetable = self.timetables.get(robot_id)
        stn = copy.deepcopy(timetable.stn)

        stn.add_task(allocation_info.new_task, allocation_info.insertion_point)
        if allocation_info.next_task:
            stn.update_task(allocation_info.next_task)

        try:
            timetable.dispatchable_graph = timetable.compute_dispatchable_graph(stn)

        except NoSTPSolution:
            self.logger.warning("The STN is inconsistent with task %s in insertion point %s", task.task_id,
                                allocation_info.insertion_point)
            self.logger.debug("STN robot %s: %s", robot_id, timetable.stn)
            self.logger.debug("Dispatchable graph robot %s: %s", robot_id, timetable.dispatchable_graph)

            raise InvalidAllocation(task.task_id, robot_id, allocation_info.insertion_point)

        timetable.add_stn_task(allocation_info.new_task)
        if allocation_info.next_task:
            timetable.add_stn_task(allocation_info.next_task)

        timetable.stn = stn
        self.timetables.update({robot_id: timetable})
        timetable.store()

        self.logger.debug("STN robot %s: %s", robot_id, timetable.stn)
        self.logger.debug("Dispatchable graph robot %s: %s", robot_id, timetable.dispatchable_graph)
        return timetable

