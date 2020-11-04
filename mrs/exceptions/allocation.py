class AlternativeTimeSlot(Exception):

    def __init__(self, bid):
        """
        Raised when a task could not be allocated at the desired time slot.

        bid (obj): winning bid for the alternative timeslot
        """
        Exception.__init__(self, bid)
        self.bid = bid


class NoAllocation(Exception):

    def __init__(self, round_id, tasks):
        """ Raised when no allocation was possible in round_id for the tasks in tasks

        """
        Exception.__init__(self, round_id, tasks)
        self.round_id = round_id
        self.tasks = tasks


class InvalidAllocation(Exception):

    def __init__(self, task_id, robot_id, insertion_point):
        """ Raised when a winning bid produces an invalid allocation

        """
        self.task_id = task_id
        self.robot_id = robot_id
        self.insertion_point = insertion_point


class TaskNotFound(Exception):
    def __init__(self, position):
        """ Raised when attempting to read a task in a timetable position that does not exist"""
        self.position = position