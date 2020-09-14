from fmlib.models import tasks
from mrs.utils.as_dict import AsDictMixin
from ropod.utils.timestamp import TimeStamp
from ropod.utils.uuid import generate_uuid


class TaskAnnouncement(AsDictMixin):
    def __init__(self, tasks, round_id, ztp, earliest_admissible_time):
        """
        Constructor for the TaskAnnouncement object

        Args:
             tasks (list): List of Task objects to be announced
             round_id (str): A string of the format UUID that identifies the round
             ztp (TimeStamp): Zero Time Point. Origin time to which task temporal information must be
                                        referenced to
        """
        self.tasks = tasks

        if not round_id:
            self.round_id = generate_uuid()
        else:
            self.round_id = round_id

        self.earliest_admissible_time = earliest_admissible_time
        self.ztp = ztp

    def to_dict(self):
        dict_repr = super().to_dict()
        tasks_dict = dict()
        for task in self.tasks:
            tasks_dict[str(task.task_id)] = task.to_dict()
            tasks_dict[str(task.task_id)].update(request=task.request.to_dict())
        dict_repr.update(tasks=tasks_dict)
        return dict_repr

    @classmethod
    def to_attrs(cls, dict_repr):
        attrs = super().to_attrs(dict_repr)
        tasks_ = list()
        for task_id, task_dict in attrs.get("tasks").items():
            task_type = task_dict.pop("_cls").split('.')[-1]
            task_cls = getattr(tasks, task_type)
            tasks_.append(task_cls.from_payload(task_dict, save_in_db=False))
        attrs.update(tasks=tasks_)
        return attrs

    @property
    def meta_model(self):
        return "task-announcement"

