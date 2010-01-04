"""
Wraps interfaces modules to work with pipeline engine
"""
import os
import sys
from tempfile import mkdtemp
from copy import deepcopy
import logging

from nipype.utils.filemanip import (copyfiles,fname_presuffix, cleandir,
                                    filename_to_list, list_to_filename)
from nipype.interfaces.base import Bunch, InterfaceResult, CommandLine
from nipype.interfaces.fsl import FSLCommand
from nipype.utils.filemanip import save_json
import nipype.pipeline.engine as pe

logger = logging.getLogger('nodewrapper')

class NodeWrapper(object):
    """
    Base class wrapper for interface objects to create nodes that can
    be used in the pipeline engine.

    Parameters
    ----------
    interface : interface object
        node specific interface  (fsl.Bet(), spm.Coregister())
    iterables : generator
        key and items to iterate using the pipeline engine
        for example to iterate over different frac values in fsl.Bet()
        node.iterables = dict(frac=lambda:[0.5,0.6,0.7])
    iterfield : 1+-element list
        key(s) over which to repeatedly call the interface.
        for example, to iterate FSL.Bet over multiple files, one can
        set node.iterfield = ['infile'].  If this list has more than 1 item
        then the inputs are selected in order simultaneously from each of these
        fields and each field will need to have the same number of members.
    base_directory : directory
        base output directory (will be hashed before creations)
        default=None, which results in the use of mkdtemp
    diskbased : Boolean
        Whether the underlying object requires disk space for
        operation and storage of output
    overwrite : Boolean
        Whether to overwrite contents of output directory if it
        already exists. If directory exists and hash matches it
        assumes that process has been executed
    name : string
        Name of this node. By default node is named
        modulename.classname. But when the same class is being used
        several times, a different name ensures that output directory
        is not overwritten each time the same functionality is run.
    
    Notes
    -----
    creates output directory
    copies/discovers files to work with
    saves a hash.json file to indicate that a process has been completed

    Examples
    --------
    >>> import nipype.interfaces.spm as spm
    >>> realign = NodeWrapper(interface=spm.Realign(), base_directory='test2', \
            diskbased=True)
    >>> realign.inputs.infile = os.path.abspath('data/funcrun.nii')
    >>> realign.inputs.register_to_mean = True
    >>> realign.run() # doctest: +SKIP

    """
    def __init__(self, interface=None,
                 iterables={}, iterfield=[],
                 diskbased=False, base_directory=None,
                 overwrite=False,
                 name=None):
        # interface can only be set at initialization
        if interface is None:
            raise Exception('Interface must be provided')
        self._interface  = interface
        self._result     = None
        self.iterables  = iterables
        self.iterfield  = iterfield
        self.parameterization = None
        self.disk_based = diskbased
        self.output_directory_base  = None
        self.overwrite = None
        if not self.disk_based:
            if base_directory is not None:
                msg = 'Setting base_directory requires a disk based interface'
                raise ValueError(msg)
        if self.disk_based:
            self.output_directory_base  = base_directory
            self.overwrite = overwrite
        if name is None:
            cname = interface.__class__.__name__
            mname = interface.__class__.__module__.split('.')[-1]
            self.name = '.'.join((cname, mname))
        else:
            self.name = name
        # for compatibility with node expansion using iterables
        self.id = self.name

    @property
    def interface(self):
        return self._interface
    
    @property
    def result(self):
        return self._result

    @property
    def inputs(self):
        return self._interface.inputs

    def set_input(self, parameter, val):
        """ Set interface input value or nodewrapper attribute

        Priority goes to interface.
        """
        if hasattr(self._interface.inputs, parameter):
            setattr(self._interface.inputs, parameter, deepcopy(val))
        else:
            setattr(self, parameter, deepcopy(val))

    def get_output(self, parameter):
        val = None
        if self._result:
            if hasattr(self._result.outputs, parameter):
                val = getattr(self._result.outputs, parameter)
            else:
                val = getattr(self, parameter)
        return val

    def check_outputs(self, parameter):
        return hasattr(self, parameter) or \
            hasattr(self._interface.outputs(), parameter)
    
    def check_inputs(self, parameter):
        return hasattr(self._interface.inputs, parameter) or \
            hasattr(self, parameter)

    def _save_hashfile(self, hashfile, hashed_inputs):
        try:
            save_json(hashfile, hashed_inputs)
        except (IOError, TypeError):
            err_type = sys.exc_info()[0]
            if err_type is TypeError:
                # XXX - SG current workaround is to just
                # create the hashed file and not put anything
                # in it
                fd = open(hashfile,'wt')
                fd.writelines(str(hashed_inputs))
                fd.close()
                logger.warn('Unable to write a particular type to the json '\
                                'file') 
            else:
                logger.critical('Unable to open the file in write mode: %s'% \
                                    hashfile)
        
        
    def run(self,updatehash=None):
        """Executes an interface within a directory.
        """
        # print "\nInputs:\n" + str(self.inputs) +"\n"
        # check to see if output directory and hash exist
        logger.info("Executing node: %s"%self.id)
        if self.disk_based:
            outdir = self._output_directory()
            outdir = self._make_output_dir(outdir)
            logger.info("in dir: %s"%outdir)
            # Get a dictionary with hashed filenames and a hashvalue
            # of the dictionary itself.
            hashed_inputs, hashvalue = self.inputs._get_bunch_hash()
            hashfile = os.path.join(outdir, '_0x%s.json' % hashvalue)
            if updatehash:
                logger.info("Updating hash: %s"%hashvalue)
                self._save_hashfile(hashfile,hashed_inputs)
            if not updatehash and (self.overwrite or not os.path.exists(hashfile)):
                logger.info("Node hash: %s"%hashvalue)
                logger.debug("continuing to execute\n")
                cleandir(outdir)
                # copy files over and change the inputs
                if hasattr(self._interface,'get_input_info'):
                    for info in self._interface.get_input_info():
                        files = self.inputs.get(info.key)
                        if files is not None:
                            infiles = filename_to_list(files)
                            newfiles = copyfiles(infiles, [outdir], copy=info.copy)
                            setattr(self.inputs, info.key, list_to_filename(newfiles))
                self._run_interface(execute=True, cwd=outdir)
                if isinstance(self._result.runtime, list):
                    # XXX In what situation is runtime ever a list?
                    # Normally it's a Bunch.
                    # Ans[SG]: Runtime is a list when we are iterating
                    # over an input field using iterfield 
                    returncode = 0
                    for r in self._result.runtime:
                        returncode = max(r.returncode, returncode)
                else:
                    returncode = self._result.runtime.returncode
                if returncode == 0:
                    self._save_hashfile(hashfile,hashed_inputs)
                else:
                    msg = "Could not run %s" % self.name
                    msg += "\nwith inputs:\n%s" % self.inputs
                    msg += "\n\tstderr: %s" % self._result.runtime.stderr
                    raise StandardError(msg)
            else:
                logger.debug("Hashfile exists. Skipping execution\n")
                # change the inputs
                if hasattr(self._interface,'get_input_info'):
                    for info in self._interface.get_input_info():
                        files = self.inputs.get(info.key)
                        if files:
                            if isinstance(files,list):
                                infiles = files
                            else:
                                infiles = [files]
                            for i,f in enumerate(infiles):
                                newfile = fname_presuffix(f, newpath=outdir)
                                if not os.path.exists(newfile):
                                    copyfiles(f, newfile, copy=info.copy)
                                if isinstance(files,list):
                                    self.inputs.get(info.key)[i] = newfile
                                else:
                                    setattr(self.inputs, info.key, newfile)
                self._run_interface(execute=False, cwd=outdir)
        else:
            self._run_interface(execute=True)
        return self._result

    # XXX This function really seriously needs to check returncodes and similar
    def _run_interface(self, execute=True, cwd=None):
        old_cwd = os.getcwd()
        if cwd:
            os.chdir(cwd)
        if not cwd and self.disk_based:
            cwd = self._output_directory()
            os.chdir(cwd)
        basewd = cwd
        if self.iterfield:
            # This branch of the if takes care of iterfield and
            # basically calls the underlying interface each time
            itervals = {}
            notlist = {} 
            for field in self.iterfield:
                itervals[field] = self.inputs.get(field)
                notlist[field] = False
                if not isinstance(itervals[field], list):
                    notlist[field] = True
                    itervals[field] = [itervals[field]]
            self._result = InterfaceResult(interface=[], runtime=[],
                                           outputs=Bunch())
            for i,v in enumerate(itervals[self.iterfield[0]]):
                if self.disk_based:
                    subdir = os.path.join(basewd,'%s_%d'%(self.iterfield[0], i))
                    if not os.path.exists(subdir):
                        os.mkdir(subdir)
                    os.chdir(subdir)
                    cwd = subdir
                    logger.debug("subdir: %s"%subdir)
                for field in self.iterfield:
                    if self.disk_based:
                        newfile = list_to_filename(copyfiles(itervals[field][i], [subdir], copy=False))
                    else:
                        newfile = itervals[field][i]
                    self.set_input(field, newfile)
                    logger.debug("iterating %s on %s: %s\n"%(self.name,
                                                             field,
                                                             newfile))
                if issubclass(self._interface.__class__,CommandLine):
                    logger.info('cmd: %s'%self._interface.cmdline)
                if execute:
                    # Passing cwd in here is redundant, even for FSLCommand
                    # instances. For FSLCommand, this is an example of another
                    # way we might do it if we decide to ditch the setwd
                    # approach. Again - something should be removed when we
                    # settle on a solution
                    result = self._interface.run(cwd=cwd)
                    if result.runtime.returncode:
                        self._itervals = itervals
                        raise RuntimeError(result.runtime.stderr)
                    self._result.interface.insert(i, result.interface)
                    self._result.runtime.insert(i, result.runtime)
                    outputs = result.outputs
                else:
                    # Could also pass in cwd here...
                    outputs = self._interface.aggregate_outputs()
                for key,val in outputs.iteritems():
                    try:
                        # This has funny default behavior if the length of the
                        # list is < i - 1. I'd like to simply use append... feel
                        # free to second my vote here!
                        self._result.outputs.get(key).insert(i, val)
                    except AttributeError:
                        # .insert(i, val) is equivalent to the following if
                        # outputs.key == None, so this is far less likely to
                        # produce subtle errors down the road!
                        setattr(self._result.outputs, key, [val])
            # restore input state
            for field in self.iterfield:
                if notlist[field]:
                    self.set_input(field, itervals[field].pop())
                else:
                    self.set_input(field, itervals[field])
        else:
            if issubclass(self._interface.__class__,CommandLine):
                logger.info('cmd: %s'%self._interface.cmdline)
            if execute:
                self._result = self._interface.run(cwd=cwd)
                if self._result.runtime.returncode:
                    raise RuntimeError(self._result.runtime.stderr)
            else:
                # Likewise, cwd could go in here
                logger.info("Not running command. just collecting outputs:")
                aggouts = self._interface.aggregate_outputs()
                self._result = InterfaceResult(interface=None,
                                               runtime=None,
                                               outputs=aggouts)
        if cwd:
            os.chdir(old_cwd)

    def update(self, **opts):
        self.inputs.update(**opts)
        
    def hash_inputs(self):
        """Computes a hash of the input fields of the underlying
        interface."""
        hashed_inputs, hashvalue = self.inputs._get_bunch_hash()
        return hashvalue

    def _output_directory(self):
        if self.output_directory_base is None:
            self.output_directory_base = mkdtemp()
        return os.path.abspath(os.path.join(self.output_directory_base,
                                            self.name))

    def _make_output_dir(self, outdir):
        """Make the output_dir if it doesn't exist.
        """
        if not os.path.exists(os.path.abspath(outdir)):
            # XXX Should this use os.makedirs which will make any
            # necessary parent directories?  I didn't because the one
            # case where mkdir failed because a missing parent
            # directory, something went wrong up-stream that caused an
            # invalid path to be passed in for `outdir`.
            os.mkdir(outdir)
        return outdir

    def __repr__(self):
        return self.id

