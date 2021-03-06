
import numpy as np
from numba import cuda

from . import cudautils, utils
from .serialize import register_distributed_serializer


class Buffer(object):
    """A 1D gpu buffer.
    """
    _cached_ipch = None

    @classmethod
    def from_empty(cls, mem):
        """From empty device array
        """
        return cls(mem, size=0, capacity=mem.size)

    @classmethod
    def null(cls, dtype):
        """Create a "null" buffer with a zero-sized device array.
        """
        mem = cuda.device_array(0, dtype=dtype)
        return cls(mem, size=0, capacity=0)

    def __init__(self, mem, size=None, capacity=None, categorical=False):
        if size is None:
            if categorical:
                size = len(mem)
            else:
                size = mem.size
        if capacity is None:
            capacity = size
        self.mem = cudautils.to_device(mem)
        _BufferSentry(self.mem).ndim(1)
        self.size = size
        self.capacity = capacity
        self.dtype = self.mem.dtype

    def serialize(self, serialize, context=None):
        """Called when dask.distributed is performing a serialization on this
        object.

        Do not use this directly.  It is invoked by dask.distributed.

        Parameters
        ----------

        serialize : callable
             Used to serialize data that needs serialization .
        context : dict; optional
            If not ``None``, it contains information about the destination.

        Returns
        -------
        (header, frames)
            See custom serialization documentation in dask.distributed.
        """
        from .serialize import should_use_ipc

        # Use destination info to determine if we should do IPC.
        use_ipc = should_use_ipc(context)
        header = {}
        # Should use IPC transfer
        if use_ipc:
            # Reuse IPC handle from previous call?
            if self._cached_ipch is not None:
                ipch = self._cached_ipch
            else:
                # Get new IPC handle
                ipch = self.to_gpu_array().get_ipc_handle()
            header['kind'] = 'ipc'
            header['mem'], frames = serialize(ipch)
            # Keep IPC handle alive
            self._cached_ipch = ipch
        # Not using IPC transfer
        else:
            header['kind'] = 'normal'
            # Serialize the buffer as a numpy array
            header['mem'], frames = serialize(self.to_array())
        return header, frames

    @classmethod
    def deserialize(cls, deserialize, header, frames):
        """Called when dask.distributed is performing a deserialization for
        data of this class.

        Do not use this directly.  It is invoked by dask.distributed.

        Parameters
        ----------

        deserialize : callable
             Used to deserialize data that needs further deserialization .
        header, frames : dict
            See custom serialization documentation in dask.distributed.

        Returns
        -------
        obj : Buffer
            Returns an instance of Buffer.
        """
        # Using IPC?
        if header['kind'] == 'ipc':
            ipch = deserialize(header['mem'], frames)
            # Open IPC handle
            with ipch as data:
                # Copy remote data over
                mem = cuda.device_array_like(data)
                mem.copy_to_device(data)
        # Not using IPC
        else:
            # Deserialize the numpy array
            mem = deserialize(header['mem'], frames)
            mem.flags['WRITEABLE'] = True  # XXX: hack for numba to work
        return Buffer(mem)

    def __reduce__(self):
        cpumem = self.to_array()
        # Note: pickled Buffer only stores *size* element.
        return type(self), (cpumem,)

    def __sizeof__(self):
        return int(self.mem.alloc_size)

    def __getitem__(self, arg):
        if isinstance(arg, slice):
            sliced = self.to_gpu_array()[arg]
            buf = Buffer(sliced)
            buf.dtype = self.dtype  # for np.datetime64 support
            return buf
        elif isinstance(arg, int):
            arg = utils.normalize_index(arg, self.size)
            # the dtype argument is necessary for datetime64 support
            # because currently we can't pass datetime64 types into
            # cuda dev arrays, so the type of the cuda dev array is
            # an i64, and we view it as the dtype on the buffer
            return self.mem[arg].view(self.dtype)
        else:
            raise NotImplementedError(type(arg))

    @property
    def avail_space(self):
        return self.capacity - self.size

    def _sentry_capacity(self, size_needed):
        if size_needed > self.avail_space:
            raise MemoryError('insufficient space in buffer')

    def append(self, element):
        self._sentry_capacity(1)
        self.extend(np.asarray(element, dtype=self.dtype))

    def extend(self, array):
        needed = array.size
        self._sentry_capacity(needed)
        array = cudautils.astype(array, dtype=self.dtype)
        self.mem[self.size:].copy_to_device(array)
        self.size += needed

    def astype(self, dtype):
        if self.dtype == dtype:
            return self
        else:
            return Buffer(cudautils.astype(self.mem, dtype=dtype))

    def to_array(self):
        return self.to_gpu_array().copy_to_host()

    def to_gpu_array(self):
        return self.mem[:self.size]

    def copy(self):
        """Deep copy the buffer
        """
        return Buffer(mem=cudautils.copy_array(self.mem),
                      size=self.size, capacity=self.capacity)

    def as_contiguous(self):
        out = Buffer(mem=cudautils.as_contiguous(self.mem),
                     size=self.size, capacity=self.capacity)
        assert out.is_contiguous()
        return out

    def is_contiguous(self):
        return self.mem.is_c_contiguous()


class BufferSentryError(ValueError):
    pass


class _BufferSentry(object):
    def __init__(self, buf):
        self._buf = buf

    def dtype(self, dtype):
        if self._buf.dtype != dtype:
            raise BufferSentryError('dtype mismatch')
        return self

    def ndim(self, ndim):
        if self._buf.ndim != ndim:
            raise BufferSentryError('ndim mismatch')
        return self

    def contig(self):
        if not self._buf.is_c_contiguous():
            raise BufferSentryError('non contiguous')


register_distributed_serializer(Buffer)
