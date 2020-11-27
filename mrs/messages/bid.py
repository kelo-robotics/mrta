from mrs.utils.as_dict import AsDictMixin
from stn.task import Task as STNTask


class Metrics(AsDictMixin):

    def __init__(self, objective, risk=1):
        self.objective = objective
        self.risk = risk

    def __str__(self):
        to_print = ""
        to_print += "(objective: {}, risk: {})".format(self.objective, self.risk)
        return to_print

    def __lt__(self, other):
        if other is None:
            return False
        return self.risk < other.risk or (self.risk == other.risk and self.objective < other.objective)

    def __eq__(self, other):
        if other is None:
            return False
        return self.risk == other.risk and self.objective == other.objective

    @classmethod
    def from_dict(cls, dict_repr):
        return cls(**dict_repr)

    @property
    def cost(self):
        return self.risk, self.objective


class BidBase(AsDictMixin):

    def __init__(self, task_id, robot_id, round_id):
        self.task_id = task_id
        self.robot_id = robot_id
        self.round_id = round_id
        self._performance = None

    @property
    def meta_model(self):
        return "bid-base"

    def set_performance(self, performance):
        self._performance = performance

    def get_performance(self):
        return self._performance


class NoBid(BidBase):
    def __init__(self, task_id, robot_id, round_id):
        super().__init__(task_id, robot_id, round_id)

    def __str__(self):
        to_print = ""
        to_print += "NoBid(task: {}, robot: {})".format(self.task_id, self.robot_id)
        return to_print

    @property
    def meta_model(self):
        return "no-bid"


class Bid(BidBase):
    def __init__(self, task_id, robot_id, round_id, metrics, insertion_point, **kwargs):
        self.metrics = metrics
        self.insertion_point = insertion_point
        self._allocation_info = None
        self.alternative_start_time = kwargs.get("alternative_start_time")
        super().__init__(task_id, robot_id, round_id)

    def __str__(self):
        to_print = ""
        to_print += "Bid (task: {}, robot: {}, metrics: {}".format(self.task_id, self.robot_id, self.metrics)
        to_print += " insertion_point: {}".format(self.insertion_point)
        if self.alternative_start_time:
            to_print += " alternative_start_time: {}".format(self.alternative_start_time)
        else:
            to_print += ")"
        return to_print

    def __lt__(self, other):
        if other is None:
            return False
        return self.metrics < other.metrics

    def __eq__(self, other):
        if other is None:
            return False
        return self.metrics == other.metrics

    def set_stn(self, stn):
        self._allocation_info.stn = stn

    def set_dispatchable_graph(self, dispatchable_graph):
        self._allocation_info.dispatchable_graph = dispatchable_graph

    def get_allocation_info(self):
        return self._allocation_info

    def set_allocation_info(self, allocation_info):
        self._allocation_info = allocation_info

    @property
    def meta_model(self):
        return "bid"

    @classmethod
    def to_attrs(cls, dict_repr):
        attrs = super().to_attrs(dict_repr)
        attrs.update(metrics=Metrics.from_dict(dict_repr.get("metrics")))
        return attrs


class AllocationInfo(AsDictMixin):
    def __init__(self, new_task, next_task=None, prev_version_next_task=None):
        self.new_task = new_task
        self.next_task = next_task
        self.prev_version_next_task = prev_version_next_task
        self._stn = None
        self._dispatchable_graph = None

    def update_next_task(self, next_task, prev_version_next_task):
        self.next_task = next_task
        self.prev_version_next_task = prev_version_next_task

    @property
    def stn(self):
        return self._stn

    @stn.setter
    def stn(self, stn):
        self._stn = stn

    @property
    def dispatchable_graph(self):
        return self._dispatchable_graph

    @dispatchable_graph.setter
    def dispatchable_graph(self, dispatchable_graph):
        self._dispatchable_graph = dispatchable_graph

    @classmethod
    def to_attrs(cls, dict_repr):
        attrs = super().to_attrs(dict_repr)
        new_task = attrs.get("new_task")
        next_task = attrs.get("next_task")
        prev_version_next_task = attrs.get("prev_version_next_task")

        attrs.update(new_task=STNTask.from_dict(new_task))
        if next_task and prev_version_next_task:
            attrs.update(next_task=STNTask.from_dict(next_task))
            attrs.update(prev_version_next_task=STNTask.from_dict(prev_version_next_task))

        return attrs


class EligibleRobot:
    def __init__(self, robot_id):
        self.robot_id = robot_id
        self.task_ids = set()  # tasks for which the robot can place a bid
        self.bids = list()
        self.no_bids = list()

    def __str__(self):
        to_print = ""
        to_print += "robot {}, tasks:{}, bids: {}, no_bids:{}".format(self.robot_id, self.task_ids, self.bids, self.no_bids)
        return to_print

    def add_task(self, task):
        self.task_ids.add(task.task_id)

    def remove_task(self, task):
        self.task_ids.remove(task.task_id)

    def process_bid(self, bid):
        if isinstance(bid, NoBid):
            self.no_bids.append(bid)
        else:
            self.bids.append(bid)

    def clean(self):
        self.bids = list()
        self.no_bids = list()

    def placed_bid(self):
        if len(self.bids) == 1 or len(self.no_bids) == len(self.task_ids):
            return True
        return False
