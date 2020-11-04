import copy
import logging
import time
from datetime import datetime

from fmlib.models.tasks import Task
from mrs.exceptions.allocation import AlternativeTimeSlot
from mrs.exceptions.allocation import NoAllocation
from mrs.exceptions.allocation import TaskNotFound
from mrs.messages.bid import NoBid
from mrs.simulation.simulator import SimulatorInterface
from ropod.utils.uuid import generate_uuid


class RoundBidder:
    def __init__(self, round_id, stn, dispatchable_graph):
        self.round_id = round_id
        self.bids = list()
        self.no_bids = list()
        self.bid_placed = None
        self.stn = stn
        self.dispatchable_graph = dispatchable_graph


class Round(SimulatorInterface):

    def __init__(self, eligible_robots, tasks, **kwargs):
        simulator = kwargs.get('simulator')
        super().__init__(simulator)

        self.logger = logging.getLogger('mrs.auctioneer.round')
        self.eligible_robots = eligible_robots
        self.tasks = tasks

        self.closure_time = kwargs.get('closure_time')
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)
        self.id = generate_uuid()

        self.finished = True
        self.opened = False
        self.received_bids = dict()
        self.received_no_bids = dict()
        self.start_time = datetime.now().timestamp()
        self.time_to_allocate = None

    def start(self):
        """ Starts and auction round:
        - opens the round
        - marks the round as not finished

        opened: The auctioneer processes bid msgs
        closed: The auctioneer no longer processes incoming bid msgs, i.e.,
                bid msgs received after the round has closed are not
                considered in the election process

        After the round closes, the election process takes place

        finished: The election process is over, i.e., an allocation has been made
                    (or an exception has been raised)

        """
        open_time = self.get_current_time()
        self.logger.debug("Round %s opened at %s and will close at %s",
                          self.id, open_time, self.closure_time)

        self.finished = False
        self.opened = True

    def process_bid(self, payload, bid_cls):
        bid = bid_cls.from_payload(payload)
        if not self.opened:
            self.logger.warning("No round bid opened. Not processing bid..")
            return
        elif bid.round_id != self.id:
            self.logger.warning("Bid round id %s does not match current round id %s. Not processing bid ..",
                                bid.robot_id, self.id)
            return

        self.logger.debug("Processing bid %s", bid)
        self.logger.debug("Round %s will close at %s ", self.id, self.closure_time)

        if isinstance(bid, NoBid):
            self.received_no_bids[bid.task_id] = self.received_no_bids.get(bid.task_id, 0) + 1
        else:
            if bid.task_id not in self.received_bids or \
                    self.update_task_bid(bid, self.received_bids[bid.task_id]):
                self.received_bids[bid.task_id] = bid

        self.eligible_robots[bid.robot_id].process_bid(bid)

    @staticmethod
    def update_task_bid(new_bid, old_bid):
        """ Called when more than one bid is received for the same task

        :return: boolean
        """
        old_robot_id = int(old_bid.robot_id)
        new_robot_id = int(new_bid.robot_id)

        if new_bid < old_bid or (new_bid == old_bid and new_robot_id < old_robot_id):
            return True

        return False

    def all_robots_placed_bid(self):
        bidding_robots = 0
        for robot_id, eligible_robot in self.eligible_robots.items():
            if eligible_robot.placed_bid():
                bidding_robots += 1

        if bidding_robots == len(self.eligible_robots):
            return True
        return False

    def time_to_close(self):
        current_time = self.get_current_time()

        if current_time > self.closure_time or self.all_robots_placed_bid():
            self.logger.debug("Closing round %s at %s", self.id, current_time)
            self.time_to_allocate = time.time() - self.start_time
            self.opened = False
            return True

        return False

    def get_result(self):
        """ Returns the results of the mrs as a tuple

        :return: round_result

        task, robot_id, position, tasks_to_allocate = round_result

        task (obj): task allocated in this round
        robot_id (string): id of the winning robot
        position (int): position in the STN where the task was added
        tasks_to_allocate (dict): tasks left to allocate

        """

        try:
            winning_bid = self.elect_winner()

            if winning_bid.alternative_start_time:
                raise AlternativeTimeSlot(winning_bid)

            return winning_bid

        except NoAllocation:
            raise

    def finish(self):
        self.opened = False
        self.finished = True
        self.logger.debug("Round %s finished", self.id)

    def get_tasks_without_bids(self):
        tasks_without_bids = list()
        for task_id, n_no_bids in self.received_no_bids.items():
            if task_id not in self.received_bids:
                task = self.tasks.get(task_id)
                if task.constraints.hard and self.alternative_timeslots:
                    self.logger.warning("Setting soft constraints for task %s", task_id)
                    task.hard_constraints = False
                    tasks_without_bids.append(task)
        return tasks_without_bids

    def elect_winner(self):
        """ Elects the winner of the round

        :return:
        mrs(dict): key - task_id,
                          value - list of robots assigned to the task

        """
        lowest_bid = None

        for task_id, bid in self.received_bids.items():
            if lowest_bid is None \
                    or bid < lowest_bid \
                    or (bid == lowest_bid and bid.task_id < lowest_bid.task_id):
                lowest_bid = copy.deepcopy(bid)

        tasks_without_bids = self.get_tasks_without_bids()

        if lowest_bid is None:
            raise NoAllocation(self.id, tasks_without_bids)

        return lowest_bid

    def is_task_frozen(self, timetable, allocation_info):
        try:
            task_id = timetable.get_task_id(allocation_info.insertion_point)
            task_to_replace = Task.get_task(task_id)
            if task_to_replace.is_frozen():
                self.logger.warning("Task %s in position %s is frozen", task_id, allocation_info.insertion_point)
                return True
            return False
        except TaskNotFound:
            return False

    def get_time_to_allocate(self):
        return self.time_to_allocate

