# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import logging
import ntpath
from volatility.framework import interfaces, renderers, exceptions, constants
from volatility.plugins.windows import handles
from volatility.plugins.windows import pslist
from volatility.framework.configuration import requirements
from volatility.framework.renderers import format_hints
from volatility.framework.objects import utility
from typing import List, Tuple
vollog = logging.getLogger(__name__)

FILE_DEVICE_DISK = 0x7
FILE_DEVICE_NETWORK_FILE_SYSTEM = 0x14
EXTENSION_CACHE_MAP = {
    "dat": "DataSectionObject",
    "img": "ImageSectionObject",
    "vacb": "SharedCacheMap",
}

class DumpFiles(interfaces.plugins.PluginInterface):
    """Dumps cached file contents from Windows memory samples."""

    _version = (1, 0, 0)

    @classmethod
    def get_requirements(cls) -> List[interfaces.configuration.RequirementInterface]:
        # Since we're calling the plugin, make sure we have the plugin's requirements
        return [
            requirements.TranslationLayerRequirement(name='primary',
                                                     description='Memory layer for the kernel',
                                                     architectures=["Intel32", "Intel64"]),
            requirements.SymbolTableRequirement(name="nt_symbols", description="Windows kernel symbols"),
            requirements.IntRequirement(name='pid',
                                        description="Process ID to include (all other processes are excluded)",
                                        optional=True),
            requirements.IntRequirement(name='fileoffset',
                                        description="Dump a single _FILE_OBJECT at this offset",
                                        optional=True),
            requirements.PluginRequirement(name='pslist', plugin=pslist.PsList, version=(1, 0, 0)),
            requirements.PluginRequirement(name='handles', plugin=handles.Handles, version=(1, 0, 0))
        ]

    def dump_file_producer(self, file_object: interfaces.objects.ObjectInterface,
                           memory_object: interfaces.objects.ObjectInterface,
                           layer: interfaces.layers.DataLayerInterface,
                           desired_file_name: str) -> str:
        """Produce a file from the memory object's get_available_pages() interface.

        :param file_object: the parent _FILE_OBJECT
        :param memory_object: the _CONTROL_AREA or _SHARED_CACHE_MAP
        :param layer: the memory layer to read from
        :param desired_file_name: name of the output file
        :return: result status
        """
        filedata = interfaces.plugins.FileInterface(desired_file_name)
        try:
            # Description of these variables:
            #   memoffset: offset in the specified layer where the page begins
            #   fileoffset: write to this offset in the destination file
            #   datasize: size of the page
            for memoffset, fileoffset, datasize in memory_object.get_available_pages():
                data = layer.read(memoffset, datasize, pad = True)
                filedata.data.seek(fileoffset)
                filedata.data.write(data)

            # Avoid writing files to disk if they are going to be empty or all zeros.
            cached_length = len(filedata.data.getvalue())
            if cached_length == 0 or filedata.data.getvalue().count(0) == cached_length:
                result_text = "No data is cached for the file at {0:#x}".format(file_object.vol.offset)
            else:
                self.produce_file(filedata)
                result_text = "Stored {}".format(filedata.preferred_filename)
        except exceptions.InvalidAddressException:
            result_text = "Unable to dump file at {0:#x}".format(file_object.vol.offset)

        return result_text

    def process_file_object(self, file_obj: interfaces.objects.ObjectInterface) -> Tuple:
        """Given a FILE_OBJECT, dump data to separate files for each of the three file caches.

        :param file_object: the FILE_OBJECT
        """

        # Filtering by these types of devices prevents us from processing other types of devices that
        # use the "File" object type, such as \Device\Tcp and \Device\NamedPipe.
        if file_obj.DeviceObject.DeviceType not in [FILE_DEVICE_DISK, FILE_DEVICE_NETWORK_FILE_SYSTEM]:
            vollog.log(constants.LOGLEVEL_VVV,
                       "The file object at {0:#x} is not a file on disk".format(file_obj.vol.offset))
            return

        # Depending on the type of object (DataSection, ImageSection, SharedCacheMap) we may need to
        # read from the memory layer or the primary layer.
        memory_layer = self.context.layers["memory_layer"]
        primary_layer = self.context.layers[self.config["primary"]]

        obj_name = file_obj.file_name_with_device()

        # This stores a list of tuples, describing what to dump and how to dump it.
        # Ex: (
        #     memory_object with get_available_pages() API (either CONTROL_AREA or SHARED_CACHE_MAP),
        #     layer to read from,
        #     file extension to apply,
        #     )
        dump_parameters = []

        # The DataSectionObject and ImageSectionObject caches are handled in basically the same way.
        # We carve these "pages" from the memory_layer.
        for member_name, extension in [("DataSectionObject", "dat"), ("ImageSectionObject", "img")]:
            try:
                section_obj = getattr(file_obj.SectionObjectPointer, member_name)
                control_area = section_obj.dereference().cast("_CONTROL_AREA")
                if control_area.is_valid():
                    dump_parameters.append((control_area, memory_layer, extension))
            except exceptions.InvalidAddressException:
                vollog.log(constants.LOGLEVEL_VVV,
                           "{0} is unavailable for file {1:#x}".format(member_name, file_obj.vol.offset))

        # The SharedCacheMap is handled differently than the caches above.
        # We carve these "pages" from the primary_layer.
        try:
            scm_pointer = file_obj.SectionObjectPointer.SharedCacheMap
            shared_cache_map = scm_pointer.dereference().cast("_SHARED_CACHE_MAP")
            if shared_cache_map.is_valid():
                dump_parameters.append((shared_cache_map, primary_layer, "vacb"))
        except exceptions.InvalidAddressException:
            vollog.log(constants.LOGLEVEL_VVV,
                       "SharedCacheMap is unavailable for file {0:#x}".format(file_obj.vol.offset))

        for memory_object, layer, extension in dump_parameters:
            cache_name = EXTENSION_CACHE_MAP[extension]
            desired_file_name = "file.{0:#x}.{1:#x}.{2}.{3}.{4}".format(file_obj.vol.offset,
                                                                        memory_object.vol.offset,
                                                                        cache_name,
                                                                        ntpath.basename(obj_name),
                                                                        extension)

            result_text = self.dump_file_producer(file_obj, memory_object, layer, desired_file_name)

            yield (cache_name, format_hints.Hex(file_obj.vol.offset),
                ntpath.basename(obj_name), # temporary, so its easier to visualize output
                result_text)

    def _generator(self, procs: List, offsets: List):
        # The handles plugin doesn't expose any staticmethod/classmethod, and it also requires stashing
        # private variables, so we need an instance (for now, anyway). We _could_ call Handles._generator()
        # to do some of the other work that is duplicated here, but then we'd need to parse the TreeGrid
        # results instead of just dealing with them as direct objects here.

        if procs:
            # Standard code for invoking the Handles() plugin from another plugin.
            handles_plugin = handles.Handles(context=self.context, config_path=self._config_path)
            type_map = handles_plugin.get_type_map(context=self.context,
                                                   layer_name=self.config["primary"],
                                                   symbol_table=self.config["nt_symbols"])
            cookie = handles_plugin.find_cookie(context=self.context,
                                                layer_name=self.config["primary"],
                                                symbol_table=self.config["nt_symbols"])

            for proc in procs:
                try:
                    object_table = proc.ObjectTable
                except exceptions.InvalidAddressException:
                    vollog.log(constants.LOGLEVEL_VVV,
                               "Cannot access _EPROCESS.ObjectTable at {0:#x}".format(proc.vol.offset))
                    continue

                for entry in handles_plugin.handles(object_table):
                    try:
                        obj_type = entry.get_object_type(type_map, cookie)
                        if obj_type == "File":
                            file_obj = entry.Body.cast("_FILE_OBJECT")
                            for result in self.process_file_object(file_obj):
                                yield (0, result)
                    except exceptions.InvalidAddressException:
                        vollog.log(constants.LOGLEVEL_VVV,
                                   "Cannot extract file from _OBJECT_HEADER at {0:#x}".format(entry.vol.offset))

                # Pull file objects from the VADs. This will produce DLLs and EXEs that are
                # mapped into the process as images, but that the process doesn't have an
                # explicit handle remaining open to those files on disk.
                for vad in proc.get_vad_root().traverse():
                    try:
                        if vad.has_member("ControlArea"):
                            # Windows xp and 2003
                            file_obj = vad.ControlArea.FilePointer.dereference()
                        elif vad.has_member("Subsection"):
                            # Vista and beyond
                            file_obj = vad.Subsection.ControlArea.FilePointer.dereference().cast("_FILE_OBJECT")
                        else:
                            continue

                        if not file_obj.is_valid():
                            continue

                        for result in self.process_file_object(file_obj):
                            yield (0, result)
                    except exceptions.InvalidAddressException:
                        vollog.log(constants.LOGLEVEL_VVV,
                                   "Cannot extract file from VAD at {0:#x}".format(vad.vol.offset))

        elif offsets:
            # Now process any offsets explicitly requested by the user.
            for offset in offsets:
                try:
                    file_obj = self.context.object(self.config["nt_symbols"] + constants.BANG + "_FILE_OBJECT",
                                                   layer_name=self.config["primary"],
                                                   native_layer_name=self.config["primary"],
                                                   offset=offset)
                    for result in self.process_file_object(file_obj):
                        yield (0, result)
                except exceptions.InvalidAddressException:
                    vollog.log(constants.LOGLEVEL_VVV,
                               "Cannot extract file at {0:#x}".format(offset))

    def run(self):
        if self.config.get("fileoffset", None) is not None:
            offsets = [self.config["fileoffset"]]
            procs = []
        else:
            filter_func = pslist.PsList.create_pid_filter([self.config.get("pid", None)])
            offsets = []
            procs = pslist.PsList.list_processes(self.context,
                                                 self.config["primary"],
                                                 self.config["nt_symbols"],
                                                 filter_func=filter_func)

        return renderers.TreeGrid(
            [("Cache", str), ("FileObject", format_hints.Hex), ("FileName", str), ("Result", str)],
            self._generator(procs, offsets))