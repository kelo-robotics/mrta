from allocation.utils.uuid import generate_uuid
import logging
from ropod.utils.timestamp import TimeStamp as ts
from allocation.bid import Bid
import copy
from allocation.exceptions.no_allocation import NoAllocation


class Round(object):

    def __init__(self, **kwargs):

        self.tasks_to_allocate = kwargs.get('tasks_to_allocate', dict())
        self.round_time = kwargs.get('round_time', 0)
        self.n_robots = kwargs.get('n_robots', 0)
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.closure_time = 0
        self.id = generate_uuid()
        self.finished = True
        self.opened = False
        self.received_bids = dict()
        self.received_no_bids = dict()

    def start(self):
        open_time = ts.get_time_stamp()
        self.closure_time = ts.get_time_stamp(self.round_time)
        logging.debug("Round opened at %s and will close at %s",
                          open_time, self.closure_time)
        self.start_round()

    def start_round(self):
        self.finished = False
        self.opened = True

    def process_bid(self, bid_dict):
        bid = Bid.from_dict(bid_dict)

        logging.debug("Processing bid from robot %s, cost: %s",
                          bid.robot_id, bid.cost)

        if bid.cost != float('inf'):
            # Process a bid
            if bid.task_id not in self.received_bids or \
                    self.update_task_bid(bid, self.received_bids[bid.task_id]):

                self.received_bids[bid.task_id] = bid

        else:
            # Process a no-bid
            logging.debug("Processing a no bid")
            logging.debug("Alternative timeslots: %s ", self.alternative_timeslots)
            self.received_no_bids[bid.task_id] = self.received_no_bids.get(bid.task_id, 0) + 1

    @staticmethod
    def update_task_bid(new_bid, old_bid):
        """ Called when more than one bid is received for the same task

        :return: boolean
        """
        old_robot_id = int(old_bid.robot_id.split('_')[-1])
        new_robot_id = int(new_bid.robot_id.split('_')[-1])

        if new_bid < old_bid or (new_bid == old_bid and new_robot_id < old_robot_id):
            return True

        return False

    def close_round(self):
        current_time = ts.get_time_stamp()

        if current_time < self.closure_time:
            return False

        logging.debug("Closing round at %s", current_time)
        return True

    def get_round_results(self):
        """ Closes the round and returns the allocation of the round and
        the tasks that need to be announced in the next round

        :return:
        allocation(dict): key - task_id,
                          value - list of robots assigned to the task

        tasks_to_allocate(dict): tasks left to allocate

        """
        # Check for which tasks the constraints need to be set to soft
        if self.alternative_timeslots and self.received_no_bids:
            self.set_soft_constraints()

        try:

            winning_bid = self.elect_winner()
            allocated_task = self.tasks_to_allocate.pop(winning_bid.task_id, None)
            robot_id = winning_bid.robot_id
            position = winning_bid.stn_position

            round_result = (allocated_task, robot_id, position, self.tasks_to_allocate)
            return round_result

        except NoAllocation:
            logging.exception("No allocation made in round %s ", self.id)
            raise NoAllocation(self.id)

    def finish(self):
        self.finished = True
        logging.debug("Round finished")

    def set_soft_constraints(self):
        """ If the number of no-bids for a task is equal to the number of robots,
        set the temporal constraints be soft
        """

        for task_id, n_no_bids in self.received_no_bids.items():
            if n_no_bids == self.n_robots:
                task = self.tasks_to_allocate.get(task_id)
                task.hard_constraints = False
                self.tasks_to_allocate.update({task_id: task})
                logging.debug("Setting soft constraints for task %s", task_id)

    def elect_winner(self):
        """ Elects the winner of the round

        :return:
        allocation(dict): key - task_id,
                          value - list of robots assigned to the task

        """
        lowest_bid = Bid()

        for task_id, bid in self.received_bids.items():
            if bid < lowest_bid:
                lowest_bid = copy.deepcopy(bid)

        if lowest_bid.cost == float('inf'):
            raise NoAllocation(self.id)

        return lowest_bid

