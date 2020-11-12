import copy
import logging
import time

from fmlib.models.actions import Duration
from fmlib.models.performance import BidPerformance, TaskPerformance
from mrs.allocation.bidding_rule import bidding_rule_factory
from mrs.allocation.round import RoundBidder
from mrs.exceptions.allocation import TaskNotFound
from mrs.messages.bid import NoBid, AllocationInfo
from mrs.messages.task_announcement import TaskAnnouncement
from mrs.messages.task_contract import TaskContract, TaskContractAcknowledgment, TaskContractCancellation
from mrs.utils.time import relative_to_ztp
from pymodm.errors import DoesNotExist
from ropod.utils.logging.counter import ContextFilter
from stn.exceptions.stp import NoSTPSolution

""" Implements a variation of the the TeSSI algorithm using the bidding_rule
specified in the config file
"""


class Bidder:

    def __init__(self, robot_id, timetable, bidding_rule, auctioneer_name, **kwargs):
        """
        Includes bidder functionality for a robot in a multi-robot task-allocation auction-based
        approach

        Args:

            robot_id (int): id of the robot, e.g. 1
            timetable (obj): Temporal information about the robot's allocated tasks
            bidding_rule(str): name of the bidding rule
            auctioneer_name (str): name of the auctioneer pyre node
            kwargs:
                api (API): object that provides middleware functionality

        """
        self.robot_id = robot_id
        self.timetable = timetable
        self.bidding_rule = bidding_rule_factory.get_bidding_rule(bidding_rule, timetable)
        self.auctioneer_name = auctioneer_name

        self.api = kwargs.get('api')
        self.task_announcement = None
        self.round = None
        self.changed_timetable = False
        self.tasks = dict()

        self.logger = logging.getLogger('mrs.bidder.%s' % self.robot_id)
        self.logger.addFilter(ContextFilter())
        self.logger.debug("Bidder robot %s initialized", self.robot_id)

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            self.logger.debug("Adding %s", key)
            self.__dict__[key] = value

    def restore_task_data(self, tasks):
        self.tasks = tasks

    def task_announcement_cb(self, msg):
        payload = msg['payload']
        task_announcement = TaskAnnouncement.from_payload(payload)
        self.tasks.update({task.task_id: task for task in task_announcement.tasks})
        self.logger.debug("Received task-announcement for round %s with tasks: %s", task_announcement.round_id,
                          [task.task_id for task in task_announcement.tasks])
        self.round = None
        self.changed_timetable = False
        self.round = RoundBidder(task_announcement.round_id,
                                 self.timetable.stn,
                                 self.timetable.dispatchable_graph)
        self.task_announcement = task_announcement

    def run(self):
        if self.task_announcement:

            if self.changed_timetable:
                self.logger.warning("The timetable changed. Finishing round %s", self.round.round_id)
                self.finish_round()
                return

            try:
                task = self.task_announcement.tasks.pop(0)

                if self.robot_id in task.eligible_robots:
                    self.compute_bids(task)

            except IndexError:
                # A bid for all tasks in the task_announcement has be computed
                self.place_bids()

    def robot_is_eligible(self, task):
        return all(i in self.capabilities for i in task.capabilities) and\
               not task.request.eligible_robots or self.robot_id in task.request.eligible_robots

    def compute_bids(self, task):
        bid = self.compute_bid(task)
        if bid is not None:
            self.logger.debug("Best bid %s", bid)
            self.round.bids.append(bid)
        else:
            self.logger.warning("No bid for task %s", task.task_id)
            no_bid = NoBid(task.task_id, self.robot_id, self.round.round_id)
            self.round.no_bids.append(no_bid)

    def compute_bid(self, task):
        start_time = time.time()
        best_bid = None
        self.logger.debug("Computing bid for task %s round %s", task.task_id, self.round.round_id)

        insertion_points = self.get_insertion_points(task)
        self.logger.debug("Insertion points: %s", insertion_points)

        for insertion_point in insertion_points:

            if not self.insert_in(insertion_point):
                continue

            self.logger.debug("Computing bid for task %s in insertion_point %s", task.task_id, insertion_point)

            stn = copy.deepcopy(self.timetable.stn)

            try:
                new_stn_task = self.get_stn_task(task, stn, insertion_point)
            except TaskNotFound:
                self.logger.debug("Stop bid computation")
                return

            stn.add_task(new_stn_task, insertion_point)
            allocation_info = AllocationInfo(copy.deepcopy(new_stn_task))

            next_task_id = stn.get_task_id(insertion_point + 1)
            if next_task_id:
                next_task = self.tasks.get(next_task_id)

                next_stn_task, prev_version_next_stn_task = self.get_next_stn_task(stn, next_task, insertion_point)
                stn.update_task(next_stn_task)
                allocation_info.update_next_task(copy.deepcopy(next_stn_task), copy.deepcopy(prev_version_next_stn_task))

            self.logger.debug("STN: %s", stn)

            try:
                bid = self.bidding_rule.compute_bid(stn,
                                                    self.robot_id,
                                                    self.round.round_id,
                                                    task,
                                                    insertion_point,
                                                    allocation_info)

                self.logger.debug("Dispatchable graph: %s", bid.get_allocation_info().dispatchable_graph)
                self.logger.debug("Bid: %s", bid)

                if best_bid is None or \
                        bid < best_bid or \
                        (bid == best_bid and bid.task_id < best_bid.task_id):
                    best_bid = bid

            except NoSTPSolution:
                self.logger.debug("The STN is inconsistent with task %s in insertion_point %s", task.task_id, insertion_point)

        end_time = time.time()

        bid_performance = BidPerformance(round_id=self.round.round_id,
                                         robot_id=self.robot_id,
                                         insertion_points=insertion_points,
                                         computation_time=end_time-start_time)
        task_performance = TaskPerformance.get_task(task.task_id)
        task_performance.update_bids(bid_performance)

        return best_bid

    def get_stn_task(self, task, stn, insertion_point):
        try:
            prev_location = self.get_previous_location(stn, insertion_point)
            travel_time = self.get_travel_time(task, prev_location)
            stn_task = self.timetable.get_stn_task(task.task_id)

            if stn_task:
                new_stn_task = self.timetable.update_stn_task(stn_task,
                                                              travel_time,
                                                              task,
                                                              insertion_point)
            else:
                new_stn_task = self.timetable.to_stn_task(task, travel_time, insertion_point)
                self.timetable.add_stn_task(new_stn_task)

            return new_stn_task
        except TaskNotFound:
            raise

    def get_next_stn_task(self, stn, next_task, insertion_point):
        self.logger.debug("Updating previous location, start and travel constraints of task %s ", next_task.task_id)

        prev_version_next_stn_task = self.timetable.get_stn_task(next_task.task_id)
        prev_location = self.get_previous_location(stn, insertion_point + 1)
        travel_time = self.get_travel_time(next_task, prev_location)

        next_stn_task = self.timetable.update_stn_task(prev_version_next_stn_task,
                                                       travel_time,
                                                       next_task,
                                                       insertion_point + 1)
        return next_stn_task, prev_version_next_stn_task

    def place_bids(self):
        self.task_announcement = None
        smallest_bid = self.get_smallest_bid(self.round.bids)
        self.send_bids(smallest_bid, self.round.no_bids)

    def finish_round(self):
        no_bids = list()
        for bid in self.round.bids:
            no_bid = NoBid(bid.task_id, self.robot_id, self.round.round_id)
            no_bids.append(no_bid)
        for task in self.task_announcement.tasks:
            if self.robot_is_eligible(task):
                no_bid = NoBid(task.task_id, self.robot_id, self.round.round_id)
                no_bids.append(no_bid)
        self.task_announcement = None
        self.send_bids(no_bids=no_bids)

    def task_contract_cb(self, msg):
        payload = msg['payload']
        task_contract = TaskContract.from_payload(payload)
        if task_contract.robot_id == self.robot_id:
            self.logger.debug("Robot %s received TASK-CONTRACT", self.robot_id)

            if not self.changed_timetable:
                self.allocate_to_robot(task_contract.task_id)
                self.send_contract_acknowledgement(task_contract, accept=True)
            else:
                self.logger.warning("The timetable changed before the round was completed, "
                                    "as a result, the bid placed %s is no longer valid ",
                                    self.round.bid_placed)
                self.send_contract_acknowledgement(task_contract, accept=False)

    def send_bids(self, bid=None, no_bids=None):
        """ Sends the bid with the smallest cost
        Sends a no-bid per task that could not be accommodated in the stn

        :param bid: bid with the smallest cost
        :param no_bids: list of no bids
        """
        if no_bids:
            for no_bid in no_bids:
                self.logger.debug("Sending no bid for task %s", no_bid.task_id)
                self.send_bid(no_bid)
        if bid:
            self.round.bid_placed = bid
            self.logger.debug("Placing bid %s ", self.round.bid_placed)
            self.send_bid(bid)

    def get_insertion_points(self, task):
        """ Returns feasible insertion points, i.e. positions of tasks whose earliest and latest start times are within
        the earliest and latest start times of the given task
        """
        r_earliest_time = relative_to_ztp(self.timetable.ztp, task.start_constraint.earliest_time)
        insertion_points = self.timetable.get_insertion_points(r_earliest_time)
        # Add insertion position after last task
        n_tasks = len(self.timetable.get_tasks())
        insertion_points.append(n_tasks + 1)
        return insertion_points

    def insert_in(self, insertion_point):
        try:
            task_id = self.timetable.get_task_id(insertion_point)
            task = self.tasks.get(task_id)
            if task.is_frozen():
                self.logger.debug("Task %s is frozen "
                                  "Not computing bid for this insertion point %s", task_id, insertion_point)
                return False
            return True
        except TaskNotFound as e:
            return True
        except DoesNotExist:
            return False

    def get_previous_location(self, stn, insertion_point):
        if insertion_point == 1:
            try:
                pose = self.robot.position
                previous_location = self.get_robot_location(pose)
            except DoesNotExist:
                self.logger.error("No information about robot's location")
        else:
            previous_task_id = stn.get_task_id(insertion_point - 1)
            self.logger.debug("Previous task : %s", previous_task_id)
            if previous_task_id:
                previous_task = self.tasks.get(previous_task_id)
                previous_location = previous_task.request.finish_location
            else:
                self.logger.error("No task found at insertion point %s", insertion_point)
                raise TaskNotFound(insertion_point)

        self.logger.debug("Previous location: %s ", previous_location)
        return previous_location

    def get_robot_location(self, pose):
        """ Returns the name of the node in the map where the robot is located"""
        try:
            robot_location = self.planner.get_node(pose.x, pose.y)
        except AttributeError:
            self.logger.warning("No planner configured")
            # For now, return a known area
            robot_location = "AMK_D_L-1_C39"
        return robot_location

    def get_travel_time(self, task, previous_location):
        """ Returns time (mean, variance) to go from previous_location to task.start_location
        """
        try:
            path = self.planner.get_path(previous_location, task.request.start_location)
            mean, variance = self.planner.get_estimated_duration(path)
        except AttributeError:
            self.logger.warning("No planner configured")
            mean = 1
            variance = 0.1
        travel_time = Duration(mean=mean, variance=variance)
        self.logger.debug("Travel duration: %s", travel_time)
        return travel_time

    def previous_task_is_frozen(self, insertion_point):
        if insertion_point > 1:
            previous_task_id = self.timetable.get_task_id(insertion_point - 1)
            task = self.tasks.get(previous_task_id)
            if task.is_frozen():
                self.logger.debug("Previous task %s is frozen", previous_task_id)
                return True
        return False

    def get_smallest_bid(self, bids):
        """ Get the bid with the smallest cost among all bids.

        :param bids: list of bids
        :return: the bid with the smallest cost
        """
        self.logger.debug("Get smallest bid")
        smallest_bid = None

        for bid in bids:
            try:
                task_id = self.timetable.get_task_id(bid.insertion_point)
                task = self.tasks.get(task_id)
                if task.is_frozen():
                    self.logger.debug("Bid %s is invalid", bid)
                    continue
            except TaskNotFound:
                pass

            if smallest_bid is None or\
                    bid < smallest_bid or\
                    (bid == smallest_bid and bid.task_id < smallest_bid.task_id):

                smallest_bid = copy.deepcopy(bid)

        return smallest_bid

    def send_bid(self, bid):
        """ Creates bid_msg and sends it to the auctioneer
        """
        msg = self.api.create_message(bid)

        self.api.publish(msg, peer=self.auctioneer_name)

    def allocate_to_robot(self, task_id):
        allocation_info = self.round.bid_placed.get_allocation_info()

        task = self.tasks.get(task_id)
        travel_time_new_task = allocation_info.new_task.get_edge("travel_time")
        task.update_travel_time(mean=travel_time_new_task.mean, variance=travel_time_new_task.variance, save_in_db=False)

        if allocation_info.next_task:
            next_task = self.tasks.get(allocation_info.new_task.task_id)
            travel_time_next_task = allocation_info.next_task.get_edge("travel_time")
            next_task.update_travel_time(mean=travel_time_next_task.mean, variance=travel_time_next_task.variance, save_in_db=False)

        self.timetable.stn = allocation_info.stn
        self.timetable.dispatchable_graph = allocation_info.dispatchable_graph

        self.logger.debug("Robot %s allocated task %s in insertion point %s", self.robot_id, task_id, self.round.bid_placed.insertion_point)
        self.logger.debug("STN: \n %s", self.timetable.stn)
        self.logger.debug("Dispatchable graph: \n %s", self.timetable.dispatchable_graph)

        tasks = [task for task in self.timetable.get_tasks()]

        self.logger.debug("Tasks allocated to robot %s:%s", self.robot_id, tasks)

    def task_contract_cancellation_cb(self, msg):
        payload = msg['payload']
        cancellation = TaskContractCancellation.from_payload(payload)
        if cancellation.robot_id == self.robot_id:
            self.logger.warning("Undoing allocation of task %s", cancellation.task_id)
            self.timetable.stn = self.round.stn
            self.timetable.dispatchable_graph = self.round.dispatchable_graph

            self.logger.debug("STN: \n %s", self.timetable.stn)
            self.logger.debug("Dispatchable graph: \n %s", self.timetable.dispatchable_graph)

    def send_contract_acknowledgement(self, task_contract, accept=True):
        allocation_info = self.round.bid_placed.get_allocation_info()
        task_contract_acknowledgement = TaskContractAcknowledgment(task_contract.task_id,
                                                                   task_contract.robot_id,
                                                                   allocation_info,
                                                                   accept)
        msg = self.api.create_message(task_contract_acknowledgement)

        self.logger.debug("Robot %s sends task-contract-acknowledgement msg ", self.robot_id)
        self.api.publish(msg, groups=['TASK-ALLOCATION'])
