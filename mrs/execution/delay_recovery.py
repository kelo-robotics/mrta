import logging

from fmlib.models import tasks
from ropod.structs.status import ActionStatus as ActionStatusConst
from ropod.utils.logging.counter import ContextFilter


class RecoveryMethod:

    options = ["re-allocate", "cancel"]

    def __init__(self, name):
        self.logger = logging.getLogger('mrs.recovery.method')
        self.logger.addFilter(ContextFilter())
        self.name = self.validate_name(name)

    def validate_name(self, name):
        if name not in self.options:
            self.logger.error("Reaction %s is not available", name)
            raise ValueError(name)
        return name

    def recover(self, timetable, task, task_progress, r_assigned_time, is_consistent):
        next_task_id = timetable.get_next_task_id(task)

        if next_task_id and self.is_next_task_late(timetable, task, next_task_id, task_progress, r_assigned_time):
            task_cls = getattr(tasks, type(task).__name__)
            return task_cls(task_id=next_task_id)

    def is_next_task_late(self, timetable, task, next_task_id, task_progress, r_assigned_time):
        self.logger.debug("Checking if task %s is at risk", next_task_id)
        mean = 0
        variance = 0

        action_idx = None
        for i, action in enumerate(task.plan[0].actions):
            if action.action_id == task_progress.action_id:
                action_idx = i

        if task_progress.action_status.status == ActionStatusConst.COMPLETED:
            # The remaining actions do not include the current action
            try:
                remaining_actions = task.plan[0].actions[action_idx + 1:]
            except IndentationError:
                # No remaining actions left
                remaining_actions = []
        else:
            # The remaining actions include the current action
            remaining_actions = task.plan[0].actions[action_idx:]

        for action in remaining_actions:
            if action.estimated_duration:
                mean += action.estimated_duration.mean
                variance += action.estimated_duration.variance

        estimated_duration = mean + 2 * round(variance ** 0.5, 3)
        self.logger.debug("Remaining estimated task duration: %s ", estimated_duration)

        node_id, node = timetable.dispatchable_graph.get_node_by_type(next_task_id, 'departure')
        latest_departure_time_next_task = timetable.dispatchable_graph.get_node_latest_time(node_id)
        self.logger.debug("Latest permitted departure time of next task: %s ", latest_departure_time_next_task)

        estimated_departure_time_of_next_task = r_assigned_time + estimated_duration
        self.logger.debug("Estimated departure time of next task: %s ", estimated_departure_time_of_next_task)

        if estimated_departure_time_of_next_task > latest_departure_time_next_task:
            self.logger.warning("Task %s is at risk", next_task_id)
            return True
        else:
            self.logger.debug("Task %s is not at risk", next_task_id)
            return False


class Corrective(RecoveryMethod):

    """ Maps allocation methods with their available corrective measures """

    def __init__(self, name):
        super().__init__(name)

    def recover(self, timetable, task, task_progress, r_assigned_time, is_consistent):
        """ React only if the last assignment was inconsistent
        """
        if is_consistent:
            return None
        elif not is_consistent:
            return super().recover(timetable, task, task_progress, r_assigned_time, is_consistent)


class Preventive(RecoveryMethod):

    """ Maps allocation methods with their available preventive measures """

    def __init__(self, name):
        super().__init__(name)

    def recover(self, timetable, task, task_progress, r_assigned_time, is_consistent):
        """ React both, when the last assignment was consistent and when it was inconsistent
        """

        return super().recover(timetable, task, task_progress, r_assigned_time, is_consistent)


class RecoveryMethodFactory:

    def __init__(self):
        self._recovery_methods = dict()

    def register_recovery_method(self, recovery_type, recovery_method):
        self._recovery_methods[recovery_type] = recovery_method

    def get_recovery_method(self, recovery_type):
        recovery_method = self._recovery_methods.get(recovery_type)
        if not recovery_method:
            raise ValueError(recovery_type)
        return recovery_method


recovery_method_factory = RecoveryMethodFactory()
recovery_method_factory.register_recovery_method('corrective', Corrective)
recovery_method_factory.register_recovery_method('preventive', Preventive)


class DelayRecovery:
    def __init__(self, type_, method, **kwargs):
        cls_ = recovery_method_factory.get_recovery_method(type_)
        self.method = cls_(method)

    @property
    def name(self):
        return self.method.name

    def recover(self, timetable, task, task_progress, r_assigned_time, is_consistent):
        return self.method.recover(timetable, task, task_progress, r_assigned_time, is_consistent)
