import logging
from datetime import timedelta

import numpy as np
from mrs.exceptions.execution import InconsistentAssignment
from mrs.exceptions.execution import InconsistentSchedule


class Scheduler(object):
    def __init__(self, robot_id, timetable, time_resolution, **kwargs):
        self.robot_id = robot_id
        self.timetable = timetable
        self.time_resolution = time_resolution
        self.logger = logging.getLogger("mrs.scheduler")
        self.logger.debug("Scheduler initialized %s", self.robot_id)

    def get_times(self, earliest_time, latest_time):
        start_times = list(np.arange(earliest_time, latest_time + self.time_resolution, self.time_resolution))
        if len(start_times) < 1:
            start_times = [earliest_time]
        elif start_times[-1] > latest_time:
            start_times.pop()
        return start_times

    def schedule(self, task):
        node_id, node = self.timetable.stn.get_node_by_type(task.task_id, 'departure')
        earliest_start_time = self.timetable.dispatchable_graph.get_node_earliest_time(node_id)
        latest_start_time = self.timetable.dispatchable_graph.get_node_latest_time(node_id)
        start_times = self.get_times(earliest_start_time, latest_start_time)

        for start_time in start_times:
            try:
                self.timetable.assign_timepoint(start_time, node_id)
                start_time = (self.timetable.ztp + timedelta(seconds=start_time)).to_datetime()
                task.schedule(start_time, latest_start_time)
                self.logger.debug("Task %s scheduled to start at %s", task.task_id, task.start_time)
                return task

            except InconsistentAssignment as e:
                self.logger.warning("Task %s could not be scheduled at %s", e.task_id, e.assigned_time)

        self.logger.warning("Task %s could not be scheduled between %s and %s", task.task_id,
                            earliest_start_time, latest_start_time)
        raise InconsistentSchedule(earliest_start_time, latest_start_time)
