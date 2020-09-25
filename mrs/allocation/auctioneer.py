import logging
from datetime import timedelta

from fmlib.models.performance import TaskPerformance, RobotPerformance
from fmlib.models.tasks import Task
from mrs.allocation.round import Round
from mrs.exceptions.allocation import AlternativeTimeSlot
from mrs.exceptions.allocation import InvalidAllocation
from mrs.exceptions.allocation import NoAllocation
from mrs.messages.bid import Bid, NoBid
from mrs.messages.bid import EligibleRobot
from mrs.messages.task_announcement import TaskAnnouncement
from mrs.messages.task_contract import TaskContract, TaskContractAcknowledgment, TaskContractCancellation
from mrs.simulation.simulator import SimulatorInterface
from mrs.utils.time import to_timestamp
from ropod.structs.status import TaskStatus as TaskStatusConst

""" Implements a variation of the the TeSSI algorithm using the bidding_rule 
specified in the config file
"""


class Auctioneer(SimulatorInterface):

    def __init__(self, timetable_manager, closure_window=30, **kwargs):
        simulator = kwargs.get('simulator')
        super().__init__(simulator)

        self.logger = logging.getLogger("mrs.auctioneer")
        self.api = kwargs.get('api')
        self.ccu_store = kwargs.get('ccu_store')
        self.robots = dict()
        self.timetable_manager = timetable_manager

        self.closure_window = timedelta(seconds=closure_window)
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.logger.debug("Auctioneer started")

        self.tasks_to_allocate = dict()
        self.allocated_tasks = dict()
        self.allocations = list()
        self.winning_bid = None
        self.changed_timetable = list()
        self.waiting_for_user_confirmation = list()
        self.round = Round(list(), self.tasks_to_allocate)

    def configure(self, **kwargs):
        api = kwargs.get('api')
        ccu_store = kwargs.get('ccu_store')
        if api:
            self.api = api
        if ccu_store:
            self.ccu_store = ccu_store

    def register_robot(self, robot):
        self.logger.debug("Registering robot %s", robot.robot_id)
        self.robots[robot.robot_id] = robot
        self.timetable_manager.register_robot(robot.robot_id)

    def unregister_robot(self, robot):
        self.logger.warning("Unregistering robot %s", robot.robot_id)
        self.robots.pop(robot.robot_id)
        allocated = Task.get_tasks(robot.robot_id, TaskStatusConst.ALLOCATED)
        scheduled = Task.get_tasks(robot.robot_id, TaskStatusConst.SCHEDULED)
        tasks_to_re_allocate = allocated + scheduled
        self.logger.warning("The following tasks will be re-allocated: %s",
                            [task.task_id for task in tasks_to_re_allocate])
        return tasks_to_re_allocate

    def set_ztp(self, time_):
        self.timetable_manager.ztp = time_

    def run(self):
        if self.tasks_to_allocate and self.round.finished:
            if not self.robots:
                self.logger.warning("There are no robots registered on the FMS."
                                    "Do not announcing tasks for allocation.")
                return

            tasks = list(self.tasks_to_allocate.values())
            self.announce_tasks(tasks)

        if self.round.opened and self.round.time_to_close():
            try:
                round_result = self.round.get_result()
                self.process_round_result(round_result)

            except NoAllocation as e:
                self.logger.warning("No allocation made in round %s ", e.round_id)
                self.tasks_to_allocate = e.tasks_to_allocate
                self.finish_round()

            except AlternativeTimeSlot as e:
                self.process_alternative_timeslot(e)

    def process_round_result(self, round_result):
        self.winning_bid, self.tasks_to_allocate = round_result

        earliest_departure_time_is_valid = \
            self.is_valid_time(self.winning_bid.departure_time.earliest_time)

        latest_departure_time_is_valid = \
            self.is_valid_time(self.winning_bid.departure_time.latest_time)

        if earliest_departure_time_is_valid or latest_departure_time_is_valid:
            self.send_task_contract(self.winning_bid.task_id, self.winning_bid.robot_id)
        else:
            self.logger.warning("The departure time of task %s is invalid", self.winning_bid.task_id)
            self.finish_round()

    def process_alternative_timeslot(self, exception):
        bid = exception.bid
        self.tasks_to_allocate = exception.tasks_to_allocate
        alternative_allocation = (bid.task_id, [bid.robot_id], bid.alternative_start_time)

        self.logger.debug("Alternative timeslot for task %s: robot %s, alternative start time: %s ", bid.task_id,
                          bid.robot_id, bid.alternative_start_time)

        self.waiting_for_user_confirmation.append(alternative_allocation)

        # TODO: Prompt the user to accept the alternative timeslot
        # For now, accept always
        self.winning_bid = bid
        self.send_task_contract(bid.task_id, bid.robot_id)

    def process_allocation(self):
        task = self.tasks_to_allocate.pop(self.winning_bid.task_id)
        allocation_info = self.winning_bid.get_allocation_info()
        try:
            timetable = self.timetable_manager.update_timetable(self.winning_bid.robot_id,
                                                                allocation_info,
                                                                task)
        except InvalidAllocation as e:
            self.logger.warning("The allocation of task %s to robot %s is inconsistent. Aborting allocation."
                                "Task %s will be included in next allocation round", e.task_id, e.robot_id, e.task_id)
            self.undo_allocation(allocation_info)
            self.tasks_to_allocate[task.task_id] = task
            return

        self.allocated_tasks[task.task_id] = task

        allocation = (self.winning_bid.task_id, [self.winning_bid.robot_id])
        self.logger.debug("Allocation: %s", allocation)
        self.logger.debug("Tasks to allocate %s", [task_id for task_id, task in self.tasks_to_allocate.items()])

        travel_time_new_task = allocation_info.new_task.get_edge("travel_time")
        task.update_travel_time(mean=travel_time_new_task.mean, variance=travel_time_new_task.variance)

        if allocation_info.next_task:
            next_task = Task.get_task(allocation_info.next_task.task_id)
            travel_time_next_task = allocation_info.next_task.get_edge("travel_time")
            next_task.update_travel_time(mean=travel_time_next_task.mean, variance=travel_time_next_task.variance)

        self.allocations.append(allocation)
        task_performance = TaskPerformance.get_task(self.winning_bid.task_id)
        task_performance.update_allocation(self.round.id, self.round.time_to_allocate)
        robot_performance = RobotPerformance.get_robot(self.winning_bid.robot_id, api=self.api)
        robot_performance.update_timetables(timetable)
        self.finish_round()

    def undo_allocation(self, allocation_info):
        self.logger.warning("Undoing allocation of round %s", self.round.id)
        self.send_task_contract_cancellation(self.winning_bid.task_id,
                                             self.winning_bid.robot_id,
                                             allocation_info.prev_version_next_task)
        self.finish_round()

    def allocate(self, tasks):
        if isinstance(tasks, list):
            self.logger.debug("Auctioneer received a list of tasks")
            for task in tasks:
                self.tasks_to_allocate[task.task_id] = task
        else:
            self.logger.debug("Auctioneer received one task")
            self.tasks_to_allocate[tasks.task_id] = tasks
        self.logger.debug("Tasks to allocate %s", {task_id for (task_id, task) in self.tasks_to_allocate.items()})

    def finish_round(self):
        self.logger.debug("Finishing round %s", self.round.id)
        self.round.finish()

    def announce_tasks(self, tasks):
        tasks_to_announce = list()
        eligible_robots = dict()

        for task in tasks:
            self.check_task_validity(task)
            eligible_robots_for_task = self.get_eligible_robots(task)

            if not eligible_robots_for_task:
                self.logger.warning("No eligible robots for task. "
                                    "Task %s is not announced.", task.task_id)
                continue

            for robot_id in eligible_robots_for_task:
                if robot_id not in eligible_robots:
                    eligible_robots[robot_id] = EligibleRobot(robot_id)
                eligible_robots[robot_id].add_task(task.task_id)

                if task not in tasks_to_announce:
                    tasks_to_announce.append(task)

        if not tasks_to_announce:
            return

        closure_time = self.get_current_time() + self.closure_window
        self.changed_timetable.clear()

        self.round = Round(eligible_robots,
                           self.tasks_to_allocate,
                           closure_time=closure_time,
                           alternative_timeslots=self.alternative_timeslots,
                           simulator=self.simulator)

        self.logger.debug("Auctioneer announces tasks %s", [task.task_id for task in tasks_to_announce])
        self.logger.debug("Eligible robots: %s", list(eligible_robots.keys()))

        task_announcement = TaskAnnouncement(tasks_to_announce, self.round.id, self.timetable_manager.ztp, closure_time)
        msg = self.api.create_message(task_announcement)
        self.round.start()
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def get_eligible_robots(self, task):
        capable_robots = self.get_capable_robots(task)
        if task.request.eligible_robots:
            eligible_robots = [robot_id for robot_id in capable_robots if robot_id in task.request.eligible_robots]
        else:
            eligible_robots = capable_robots
        self.logger.debug("Eligible robots for task %s: %s", task.task_id, eligible_robots)
        return eligible_robots

    def get_capable_robots(self, task):
        capable_robots = list()
        for robot_id, robot in self.robots.items():
            if all(i in robot.capabilities for i in task.capabilities):
                capable_robots.append(robot_id)
        self.logger.debug("Capable robots for task %s: %s", task.task_id, capable_robots)
        return capable_robots

    def check_task_validity(self, task):
        if not self.is_valid_time(task.start_constraint.latest_time):
            if self.alternative_timeslots:
                task.hard_constraints = False
                self.logger.warning("Setting soft constraints for task %s", task.task_id)
            else:
                self.logger.warning("Task %s cannot not be allocated at its given temporal constraints",
                                    task.task_id)
                task.update_status(TaskStatusConst.PREEMPTED)
                self.tasks_to_allocate.pop(task.task_id)

    def get_closure_time(self, tasks):
        earliest_task = Task.get_earliest_task(tasks)
        closure_time = earliest_task.start_constraint.earliest_time - self.closure_window
        if not self.is_valid_time(closure_time):
            # Closure window should be long enough to allow robots to bid (tune if necessary)
            closure_time = self.get_current_time() + self.closure_window

        return closure_time

    def update_soft_constraints(self, task):
        start_time_window = task.start_constraint.latest_time - task.start_constraint.earliest_time

        earliest_start_time = self.get_current_time() + timedelta(minutes=5)
        latest_start_time = earliest_start_time + start_time_window
        task.update_start_constraint(earliest_start_time, latest_start_time)

    def bid_cb(self, msg):
        payload = msg['payload']
        self.round.process_bid(payload, Bid)

    def no_bid_cb(self, msg):
        payload = msg['payload']
        self.round.process_bid(payload, NoBid)

    def task_contract_acknowledgement_cb(self, msg):
        payload = msg['payload']
        ack = TaskContractAcknowledgment.from_payload(payload)

        if ack.accept and ack.robot_id not in self.changed_timetable:
            self.logger.debug("Concluding allocation of task %s", ack.task_id)
            self.winning_bid.set_allocation_info(ack.allocation_info)
            self.process_allocation()

        elif ack.accept and ack.robot_id in self.changed_timetable:
            self.undo_allocation(ack.allocation_info)

        else:
            self.logger.warning("Round %s has to be repeated", self.round.id)
            self.finish_round()

    def send_task_contract_cancellation(self, task_id, robot_id, prev_version_next_task):
        task_contract_cancellation = TaskContractCancellation(task_id, robot_id, prev_version_next_task)
        msg = self.api.create_message(task_contract_cancellation)
        self.logger.debug("Cancelling contract for task %s", task_id)
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def send_task_contract(self, task_id, robot_id):
        # Send TaskContract only if the timetable of robot_id has not changed since the round opened
        if robot_id not in self.changed_timetable:
            task_contract = TaskContract(task_id, robot_id)
            msg = self.api.create_message(task_contract)
            self.api.publish(msg, groups=['TASK-ALLOCATION'])
        else:
            self.logger.warning("Round %s has to be repeated", self.round.id)
            self.finish_round()

    def get_task_schedule(self, task_id, robot_id):
        """ Returns a dict
            departure_time: earliest departure time according to the dispatchable graph
            start_time : earliest start time according to the dispatchable graph
            finish_time: latest finish time according to the dispatchable graph
        """
        timetable = self.timetable_manager.get(robot_id)

        r_earliest_departure_time = timetable.dispatchable_graph.get_time(task_id, "departure")
        r_earliest_start_time = timetable.dispatchable_graph.get_time(task_id, "start")
        r_latest_finish_time = timetable.dispatchable_graph.get_time(task_id, "finish", False)

        departure_time = to_timestamp(self.timetable_manager.ztp, r_earliest_departure_time)
        start_time = to_timestamp(self.timetable_manager.ztp, r_earliest_start_time)
        finish_time = to_timestamp(self.timetable_manager.ztp, r_latest_finish_time)

        self.logger.debug("Task %s departure time: %s", task_id, departure_time)
        self.logger.debug("Task %s earliest start time : %s", task_id, start_time)
        self.logger.debug("Task %s latest finish time: %s", task_id, finish_time)

        task_schedule = {"departure_time": departure_time.to_datetime(),
                         "start_time": start_time.to_datetime(),
                         "finish_time": finish_time.to_datetime()}

        return task_schedule

