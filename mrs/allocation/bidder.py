import copy
import logging

from fmlib.models.actions import Duration
from mrs.allocation.bidding_rule import bidding_rule_factory
from mrs.exceptions.allocation import TaskNotFound
from mrs.messages.bid import NoBid, AllocationInfo
from mrs.messages.task_announcement import TaskAnnouncement
from mrs.messages.task_contract import TaskContract, TaskContractAcknowledgment, TaskContractCancellation
from mrs.utils.time import relative_to_ztp
from pymodm.errors import DoesNotExist
from ropod.structs.status import TaskStatus as TaskStatusConst
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

            robot_id (str): id of the robot, e.g. robot_001
            bidding_rule(str): name of the bidding rule
            auctioneer_name (str): name of the auctioneer pyre node
            kwargs:
                api (API): object that provides middleware functionality
                robot_store (robot_store): interface to interact with the db

        """
        self.robot_id = robot_id
        self.timetable = timetable
        self.api = kwargs.get('api')

        self.logger = logging.getLogger('mrs.bidder.%s' % self.robot_id)

        self.bidding_rule = bidding_rule_factory.get_bidding_rule(bidding_rule, timetable)
        self.auctioneer_name = auctioneer_name
        self.bid_placed = None
        self.changed_timetable = False

        self.logger.debug("Bidder initialized %s", self.robot_id)

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            self.logger.debug("Adding %s", key)
            self.__dict__[key] = value

    def task_announcement_cb(self, msg):
        payload = msg['payload']
        task_announcement = TaskAnnouncement.from_payload(payload)
        self.tasks.update({task.task_id: task for task in task_announcement.tasks})

        self.logger.debug("Received TASK-ANNOUNCEMENT msg round %s with %s tasks", task_announcement.round_id,
                          len(task_announcement.tasks))
        self.logger.debug("Current stn: %s", self.timetable.stn)
        self.logger.debug("Current dispatchable graph: %s", self.timetable.dispatchable_graph)
        self.compute_bids(task_announcement)

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
                                    self.bid_placed)
                self.send_contract_acknowledgement(task_contract, accept=False)

    def compute_bids(self, task_announcement):
        bids = list()
        no_bids = list()
        round_id = task_announcement.round_id
        earliest_admissible_time = task_announcement.earliest_admissible_time
        self.changed_timetable = False
        self.bid_placed = None

        for task in task_announcement.tasks:
            if not task.request.eligible_robots or self.robot_id in task.request.eligible_robots:
                self.logger.debug("Computing bid of task %s round %s", task.task_id, round_id)
                best_bid = self.compute_bid(task, round_id, earliest_admissible_time)

                if best_bid:
                    self.logger.debug("Best bid %s", best_bid)
                    bids.append(best_bid)
                else:
                    self.logger.warning("No bid for task %s", task.task_id)
                    no_bid = NoBid(task.task_id, self.robot_id, round_id)
                    no_bids.append(no_bid)

        smallest_bid = self.get_smallest_bid(bids)

        self.send_bids(smallest_bid, no_bids)

    def send_bids(self, bid, no_bids):
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
            self.bid_placed = bid
            self.logger.debug("Placing bid %s ", self.bid_placed)
            self.send_bid(bid)

    def compute_bid(self, task, round_id, earliest_admissible_time):
        best_bid = None
        tasks = self.timetable.get_tasks()

        insertion_points = self.get_insertion_points(task)
        # Add insertion position after last task
        insertion_points.append(len(tasks) + 1)
        self.logger.debug("Insertion points: %s", insertion_points)

        for insertion_point in insertion_points:

            prev_version_next_stn_task = None

            self.logger.debug("Computing bid for task %s in insertion_point %s", task.task_id, insertion_point)
            if not self.insert_in(insertion_point):
                continue

            prev_location = self.get_previous_location(insertion_point)
            travel_duration = self.get_travel_duration(task, prev_location)
            if travel_duration is None:
                self.logger.warning("There was a problem computing the estimated duration between %s and %s "
                                    "Not computing bid for insertion point %s",
                                    prev_location, task.request.start_location, insertion_point)
                continue

            prev_task_is_frozen = self.previous_task_is_frozen(insertion_point)
            new_stn_task = self.timetable.to_stn_task(task, travel_duration, insertion_point, earliest_admissible_time,
                                                      prev_task_is_frozen)

            self.timetable.insert_task(new_stn_task, insertion_point)
            allocation_info = AllocationInfo(insertion_point, new_stn_task)

            try:
                # Update previous location and start constraints of next task (if any)
                next_task_id = self.timetable.get_task_id(insertion_point+1)
                next_task = self.tasks.get(next_task_id)
                self.logger.debug("Updating previous location and start constraints of task %s, ", next_task.task_id)
                prev_version_next_stn_task = self.timetable.get_stn_task(next_task.task_id)

                prev_location = self.get_previous_location(insertion_point+1)
                travel_duration = self.get_travel_duration(next_task, prev_location)

                if travel_duration is None:
                    self.logger.warning("There was a problem computing the estimated duration between %s and %s "
                                        "Not computing bid for insertion point %s",
                                        prev_location, next_task.request.start_location, insertion_point)
                    continue

                prev_task_is_frozen = self.previous_task_is_frozen(insertion_point+1)
                next_stn_task = self.timetable.update_stn_task(copy.deepcopy(prev_version_next_stn_task),
                                                               travel_duration,
                                                               insertion_point+1,
                                                               earliest_admissible_time,
                                                               prev_task_is_frozen)
                self.timetable.update_task(next_stn_task)

                allocation_info.update_next_task(next_stn_task, prev_version_next_stn_task)

            except TaskNotFound as e:
                pass

            stn = copy.deepcopy(self.timetable.stn)

            try:
                bid = self.bidding_rule.compute_bid(stn, self.robot_id, round_id, task, allocation_info)

                self.logger.debug("Bid: %s", bid)

                if best_bid is None or \
                        bid < best_bid or \
                        (bid == best_bid and bid.task_id < best_bid.task_id):
                    best_bid = bid

            except NoSTPSolution:
                self.logger.debug("The STN is inconsistent with task %s in insertion_point %s", task.task_id, insertion_point)

            self.timetable.stn.remove_task(insertion_point)

            if prev_version_next_stn_task is not None:
                self.timetable.stn.update_task(prev_version_next_stn_task)

        return best_bid

    def get_insertion_points(self, task):
        """ Returns feasible insertion points, i.e. positions of tasks whose earliest and latest start times are within
        the earliest and latest start times of the given task
        """
        r_earliest_time = relative_to_ztp(self.timetable.ztp, task.start_constraint.earliest_time)
        r_latest_time = relative_to_ztp(self.timetable.ztp, task.start_constraint.latest_time)
        insertion_points = self.timetable.get_insertion_points(r_earliest_time, r_latest_time)
        return insertion_points

    def insert_in(self, insertion_point):
        try:
            task_id = self.timetable.get_task_id(insertion_point)
            task = self.tasks.get(task_id)
            if task.status.status in [TaskStatusConst.DISPATCHED, TaskStatusConst.ONGOING]:
                self.logger.debug("Task %s was already dispatched "
                                  "Not computing bid for this insertion point %s", task_id, insertion_point)
                return False
            return True
        except TaskNotFound as e:
            return True
        except DoesNotExist:
            return False

    def get_previous_location(self, insertion_point):
        if insertion_point == 1:
            try:
                pose = self.robot.position
                previous_location = self.get_robot_location(pose)
            except DoesNotExist:
                self.logger.error("No information about robot's location")
        else:
            previous_task_id = self.timetable.get_task_id(insertion_point - 1)
            previous_task = self.tasks.get(previous_task_id)
            previous_location = previous_task.request.finish_location

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

    def get_travel_duration(self, task, previous_location):
        """ Returns time (mean, variance) to go from previous_location to task.start_location
        """
        try:
            path = self.planner.get_path(previous_location, task.request.start_location)
            mean, variance = self.planner.get_estimated_duration(path)
        except AttributeError:
            self.logger.warning("No planner configured")
            mean = 1
            variance = 0.1
        travel_duration = Duration(mean=mean, variance=variance)
        self.logger.debug("Travel duration: %s", travel_duration)
        return travel_duration

    def previous_task_is_frozen(self, insertion_point):
        if insertion_point > 1:
            previous_task_id = self.timetable.get_task_id(insertion_point - 1)
            task = self.tasks.get(previous_task_id)
            if task.status.status in [TaskStatusConst.SCHEDULED, TaskStatusConst.DISPATCHED, TaskStatusConst.ONGOING]:
                self.logger.debug("Previous task %s is frozen", previous_task_id)
                return True
        return False

    @staticmethod
    def get_smallest_bid(bids):
        """ Get the bid with the smallest cost among all bids.

        :param bids: list of bids
        :return: the bid with the smallest cost
        """
        smallest_bid = None

        for bid in bids:

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
        allocation_info = self.bid_placed.get_allocation_info()
        self.timetable.add_stn_task(allocation_info.new_task)
        if allocation_info.next_task:
            self.timetable.add_stn_task(allocation_info.next_task)

        self.timetable.stn = allocation_info.stn
        self.timetable.dispatchable_graph = allocation_info.dispatchable_graph

        self.logger.debug("Robot %s allocated task %s", self.robot_id, task_id)
        self.logger.debug("STN: \n %s", self.timetable.stn)
        self.logger.debug("Dispatchable graph: \n %s", self.timetable.dispatchable_graph)

        tasks = [task for task in self.timetable.get_tasks()]

        self.logger.debug("Tasks allocated to robot %s:%s", self.robot_id, tasks)

    def task_contract_cancellation_cb(self, msg):
        payload = msg['payload']
        cancellation = TaskContractCancellation.from_payload(payload)
        if cancellation.robot_id == self.robot_id:
            self.logger.warning("Undoing allocation of task %s", cancellation.task_id)
            self.timetable.remove_task(cancellation.task_id)

            if cancellation.prev_version_next_task:
                self.timetable.update_task(cancellation.prev_version_next_task)
                self.timetable.add_stn_task(cancellation.prev_version_next_task)

            tasks = [task for task in self.timetable.get_tasks()]
            self.logger.debug("Tasks allocated to robot %s:%s", self.robot_id, tasks)
            self.logger.debug("STN: \n %s", self.timetable.stn)

    def send_contract_acknowledgement(self, task_contract, accept=True):
        allocation_info = self.bid_placed.get_allocation_info()
        task_contract_acknowledgement = TaskContractAcknowledgment(task_contract.task_id,
                                                                   task_contract.robot_id,
                                                                   allocation_info,
                                                                   accept)
        msg = self.api.create_message(task_contract_acknowledgement)

        self.logger.debug("Robot %s sends task-contract-acknowledgement msg ", self.robot_id)
        self.api.publish(msg, groups=['TASK-ALLOCATION'])
