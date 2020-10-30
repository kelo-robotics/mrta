import uuid

from mrs.utils.time import relative_to_ztp
from stn.task import Edge
from stn.task import Task as STNTask
from stn.task import Timepoint


class STNInterface:
    def __init__(self, ztp, stn, dispatchable_graph):
        self.ztp = ztp
        self.stn = stn
        self.dispatchable_graph = dispatchable_graph
        self.stn_tasks = dict()

    def get_stn_task(self, task_id):
        if isinstance(task_id, uuid.UUID):
            task_id = str(task_id)
        return self.stn_tasks.get(task_id)

    def add_stn_task(self, stn_task):
        self.stn_tasks[str(stn_task.task_id)] = stn_task

    def insert_task(self, stn_task, insertion_point):
        self.stn.add_task(stn_task, insertion_point)

    def update_task(self, stn_task):
        self.stn.update_task(stn_task)

    def update_travel_time(self, task_id, travel_edge):
        stn_task = self.get_stn_task(task_id)
        stn_task.update_edge(travel_edge.name, travel_edge.mean, travel_edge.variance)
        self.add_stn_task(stn_task)
        self.stn.update_travel_time(stn_task)

    def update_work_time(self, task_id, work_edge):
        stn_task = self.get_stn_task(task_id)
        stn_task.update_edge(work_edge.name, work_edge.mean, work_edge.variance)
        self.add_stn_task(stn_task)
        self.stn.update_work_time(stn_task)

    def to_stn_task(self, task, travel_time, insertion_point):
        travel_edge = Edge(name="travel_time", mean=travel_time.mean, variance=travel_time.variance)
        duration_edge = Edge(name="work_time", mean=task.work_time.mean, variance=task.work_time.variance)

        start_timepoint = self.get_start_timepoint(task)
        departure_timepoint = self.get_departure_timepoint(task.start_constraint.earliest_time,
                                                           start_timepoint,
                                                           travel_edge,
                                                           insertion_point)
        finish_timepoint = self.get_finish_timepoint(start_timepoint, duration_edge)

        edges = [travel_edge, duration_edge]
        timepoints = [departure_timepoint, start_timepoint, finish_timepoint]
        start_action_id = task.plan[0].actions[0].action_id
        finish_action_id = task.plan[0].actions[-1].action_id

        stn_task = STNTask(task.task_id, timepoints, edges, start_action_id, finish_action_id)
        return stn_task

    def update_stn_task(self, stn_task, travel_time, earliest_start_time, insertion_point):
        travel_edge = Edge(name="travel_time", mean=travel_time.mean, variance=travel_time.variance)
        start_timepoint = stn_task.get_timepoint("start")
        departure_timepoint = self.get_departure_timepoint(earliest_start_time, start_timepoint, travel_edge,
                                                           insertion_point)
        stn_task.update_timepoint("departure", departure_timepoint.r_earliest_time, departure_timepoint.r_latest_time)
        stn_task.update_edge(travel_edge.name, travel_edge.mean, travel_edge.variance)
        return stn_task

    def get_departure_timepoint(self, earliest_start_time, start_timepoint, travel_edge, insertion_point):
        departure_timepoint = self.stn.get_prev_timepoint("departure", start_timepoint, travel_edge)

        if insertion_point == 1:
            departure_timepoint.r_earliest_time = relative_to_ztp(self.ztp, earliest_start_time)

        return departure_timepoint

    def get_start_timepoint(self, task):
        r_earliest_start_time = relative_to_ztp(self.ztp, task.start_constraint.earliest_time)
        r_latest_start_time = relative_to_ztp(self.ztp, task.start_constraint.latest_time)
        start_timepoint = Timepoint(name="start", r_earliest_time=r_earliest_start_time,
                                    r_latest_time=r_latest_start_time)
        return start_timepoint

    def get_finish_timepoint(self, start_timepoint, duration_edge):
        finish_timepoint = self.stn.get_next_timepoint("finish", start_timepoint, duration_edge)
        return finish_timepoint

    def get_r_time_previous_task(self, insertion_point, node_type, earliest=True):
        task_id = self.stn.get_task_id(insertion_point-1)
        return self.dispatchable_graph.get_time(task_id, node_type, earliest)
