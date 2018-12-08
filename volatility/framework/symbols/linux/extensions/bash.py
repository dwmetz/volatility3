from volatility.framework import exceptions
from volatility.framework import objects
from volatility.framework.objects import utility


class hist_entry(objects.Struct):
    def is_valid(self):
        try:
            cmd = self.get_command()
            ts = utility.array_to_string(self.timestamp.dereference())
        except exceptions.PagedInvalidAddressException:
            return False

        if not cmd or len(cmd) == 0:
            return False

        if not ts or len(ts) == 0:
            return False

        # At this point in time, the epoc integer size will
        # never be less than 10 characters, and the stamp is
        # always preceded by a pound/hash character.
        if len(ts) < 10 or str(ts)[0] != "#":
            return False

        # The final check is to make sure the entire string
        # is composed of numbers. Try to convert to an int.
        try:
            int(str(ts)[1:])
        except ValueError:
            return False

        return True

    def get_time_as_integer(self):
        # Get the string and remove the leading "#" from the timestamp
        time_string = utility.array_to_string(self.timestamp.dereference())[1:]
        # Convert the string into an integer (number of seconds)
        return int(time_string)

    def get_time_object(self):
        nsecs = self.get_time_as_integer()
        # Build a timestamp object from the integer
        return utility.unixtime_to_datetime(nsecs)

    def get_command(self):
        return utility.array_to_string(self.line.dereference())
