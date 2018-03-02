from __future__ import absolute_import
from __future__ import print_function
import sys
import os, os.path
import time
import socket
import csv
import itertools
import collections
import math
import six.moves.cPickle
import random
import bz2
import gzip
import zlib
from copy import copy
import shutil
import heapq
import struct
import traceback
import tempfile

try:
    from cStringIO import StringIO
    BytesIO = StringIO
except ImportError:
    from six import BytesIO, StringIO

try:
    from cPickle import Pickler, Unpickler
except ImportError:
    from pickle import  Pickler, Unpickler

from dpark.dependency import *
from dpark.util import (
    spawn, chain, mkdir_p, recurion_limit_breaker, atomic_file,
    AbortFileReplacement, get_logger, portable_hash, Scope, masked_crc32c,
    gzip_decompressed_fh, gzip_decompressed_fh_2, gzip_find_block
)
from dpark.shuffle import (
    Merger, CoGroupMerger, SortedShuffleFetcher, SortedMerger, CoGroupSortedMerger,
    OrderedMerger, OrderedCoGroupMerger,
    SortedGroupMerger, StreamCoGroupSortedMerger,
)
from dpark.env import env
from dpark.file_manager import open_file, CHUNKSIZE
from dpark.beansdb import BeansdbReader, BeansdbWriter
from contextlib import closing
import six
from six.moves import filter
from six.moves import map
from six.moves import range
from six.moves import zip
from functools import reduce
if not six.PY2:
    from io import TextIOWrapper

logger = get_logger(__name__)


class Split(object):
    def __init__(self, idx):
        self.index = idx


def cached(func):
    def getstate(self):
        d = getattr(self, '_pickle_cache', None)
        if d is None:
            d = func(self)
            self._pickle_cache = d
        return d
    return getstate


class RDD(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.id = RDD.newId()
        self._splits = []
        self._dependencies = []
        self.aggregator = None
        self._partitioner = None
        self.shouldCache = False
        self.checkpoint_path = None
        self._checkpoint_rdd = None
        ctx.init()
        self.err = ctx.options.err
        self.mem = ctx.options.mem
        self.cpus = 0
        self.gpus = 0
        self._preferred_locs = {}
        self.repr_name = '<%s>' % (self.__class__.__name__,)
        self.scope = Scope()

    nextId = 0

    @classmethod
    def newId(cls):
        cls.nextId += 1
        return cls.nextId

    @cached
    def __getstate__(self):
        d = dict(self.__dict__)
        d.pop('_dependencies', None)
        d.pop('_splits', None)
        d.pop('_preferred_locs', None)
        d.pop('ctx', None)
        d['_split_size'] = len(self.splits)
        return d

    def __len__(self):
        if hasattr(self, '_split_size'):
            return self._split_size
        return len(self.splits)

    def __repr__(self):
        return self.repr_name

    def __getslice__(self, i,j):
        return SliceRDD(self, i, j)

    def __getitem__(self, idx):
        if isinstance(idx, slice) and idx.step is None:
            start = idx.start or 0
            stop = idx.stop or sys.maxsize
            return self.__getslice__(start, stop)

        raise ValueError('Unsupported slicing!')

    def mergeSplit(self, splitSize=None, numSplits=None):
        return MergedRDD(self, splitSize, numSplits)

    @property
    def splits(self):
        if self._checkpoint_rdd:
            return self._checkpoint_rdd.splits
        return self._splits

    @property
    def dependencies(self):
        if self._checkpoint_rdd:
            return self._checkpoint_rdd.dependencies
        return self._dependencies

    def compute(self, split):
        raise NotImplementedError

    @property
    def partitioner(self):
        return self._partitioner

    def cache(self):
        self.shouldCache = True
        self._pickle_cache = None # clear pickle cache
        return self

    def preferredLocations(self, split):
        if self._checkpoint_rdd:
            return self._checkpoint_rdd._preferred_locs.get(split, [])

        if self.shouldCache:
            locs = env.cacheTracker.getCachedLocs(self.id, split.index)
            if locs:
                return locs
        return self._preferred_locs.get(split, [])

    def checkpoint(self, path=None):
        if path is None:
            path = self.ctx.options.checkpoint_dir
        if path:
            ident = '%d_%x' % (self.id, hash(str(self)))
            path = os.path.join(path, ident)
            mkdir_p(path)
            self.checkpoint_path = path
        else:
            logger.warning('No checkpoint will be saved without checkpoint_dir,'
                           'please re-run with --checkpoint-dir to enable checkpoint')
        return self

    def _clear_dependencies(self):
        self._dependencies = []
        self._splits = []

    def _do_checkpoint(self):
        if self.checkpoint_path:
            if not self._checkpoint_rdd:
                _generated = list(map(int, CheckpointRDD.generated_files(self.checkpoint_path)))
                if len(_generated) != len(self):
                    missing = [sp.index for sp in self.splits if sp.index not in _generated]
                    sum(self.ctx.runJob(self, lambda x:list(x), missing), [])

                self._pickle_cache = None
                self._checkpoint_rdd = CheckpointRDD(self.ctx, self.checkpoint_path)
                self._clear_dependencies()
            return False
        return True

    @recurion_limit_breaker
    def iterator(self, split):
        def _compute(rdd, split):
            if self.shouldCache:
                return env.cacheTracker.getOrCompute(rdd, split)
            else:
                return rdd.compute(split)

        if self.checkpoint_path:
            if self._checkpoint_rdd is None:
                p = os.path.join(self.checkpoint_path, str(split.index))
                v = list(self.compute(split))
                with atomic_file(p) as f:
                    f.write(six.moves.cPickle.dumps(v, -1))

                return v
            else:
                return _compute(self._checkpoint_rdd, split)
        return _compute(self, split)

    def map(self, f):
        return MappedRDD(self, f)

    def flatMap(self, f):
        return FlatMappedRDD(self, f)

    def filter(self, f):
        return FilteredRDD(self, f)

    def sample(self, faction, withReplacement=False, seed=12345):
        return SampleRDD(self, faction, withReplacement, seed)

    def union(self, *args):
        return UnionRDD(self.ctx, [self] + list(args))

    def sort(self, key=lambda x:x, reverse=False, numSplits=None, taskMemory=None):
        if not len(self):
            return self
        if len(self) == 1:
            return self.mapPartitions(lambda it: sorted(it, key=key, reverse=reverse))
        if numSplits is None:
            numSplits = min(self.ctx.defaultMinSplits, len(self))
        n = max(numSplits * 10 // len(self), 1)
        samples = self.mapPartitions(lambda x:itertools.islice(x, n)).map(key).collect()
        keys = sorted(samples, reverse=reverse)[5::10][:numSplits-1]
        parter = RangePartitioner(keys, reverse=reverse)
        aggr = MergeAggregator()
        parted = ShuffledRDD(self.map(lambda x:(key(x),x)), aggr, parter, taskMemory).flatMap(lambda x_y:x_y[1])
        return parted.mapPartitions(lambda x:sorted(x, key=key, reverse=reverse))

    def glom(self):
        return GlommedRDD(self)

    def cartesian(self, other, taskMemory=None, cacheMemory=None):
        return CartesianRDD(self, other, taskMemory=taskMemory, cacheMemory=cacheMemory)

    def zipWith(self, other):
        return ZippedRDD(self.ctx, [self, other])

    def groupBy(self, f, numSplits=None, sort_shuffle=None, iter_values=None):
        if numSplits is None:
            numSplits = min(self.ctx.defaultMinSplits, len(self))
        return self.map(lambda x: (f(x), x)).groupByKey(numSplits, sort_shuffle=sort_shuffle, iter_values=iter_values)

    def pipe(self, command, quiet=False):
        if isinstance(command, str):
            command = command.split(' ')
        return PipedRDD(self, command, quiet)

    def fromCsv(self, dialect='excel'):
        return CSVReaderRDD(self, dialect)

    def mapPartitions(self, f):
        return MapPartitionsRDD(self, f)
    mapPartition = mapPartitions

    def foreach(self, f):
        def mf(it):
            for i in it:
                f(i)
        list(self.ctx.runJob(self, mf))

    def foreachPartition(self, f):
        list(self.ctx.runJob(self, f))

    def enumeratePartition(self):
        return EnumeratePartitionsRDD(self, lambda x,it: map(lambda y:(x,y), it))

    def enumerate(self):
        """
        enumerate this RDD.

        >>> dpark.parallelize(["a", "b", "c", "d"], 3).enumerate().collect()
        [(0, 'a'), (1, 'b'), (2, 'c'), (3, 'd')]
        """
        starts = [0]
        if len(self) > 1:
            nums = self.mapPartitions(lambda it: [sum(1 for i in it)]).collect()
            for i in range(len(nums) - 1):
                starts.append(starts[-1] + nums[i])

        return EnumeratePartitionsRDD(self, lambda x,it: enumerate(it, starts[x]))

    def collect(self):
        return sum(self.ctx.runJob(self, lambda x: list(x)), [])

    def __iter__(self):
        return chain(self.ctx.runJob(self, lambda x: list(x)))

    def reduce(self, f):
        def reducePartition(it):
            logger = get_logger(__name__)
            if self.err < 1e-8:
                try:
                    return [reduce(f, it)]
                except TypeError as e:
                    empty_msg = 'reduce() of empty sequence with no initial value'
                    if not six.PY2:
                        e.message = str(e)
                    if e.message == empty_msg:
                        return []
                    else:
                        raise e

            s = None
            total, err = 0, 0
            for v in it:
                try:
                    total += 1
                    if s is None:
                        s = v
                    else:
                        s = f(s, v)
                except Exception as e:
                    logger.warning("skip bad record %s: %s", v, e)
                    err += 1
                    if total > 100 and err > total * self.err * 10:
                        raise Exception("too many error occured: %s" % (float(err)/total))

            if err > total * self.err:
                raise Exception("too many error occured: %s" % (float(err)/total))

            return [s] if s is not None else []

        return reduce(f, chain(self.ctx.runJob(self, reducePartition)))

    def uniq(self, numSplits=None, taskMemory=None, sort_shuffle=None):
        g = self.map(lambda x:(x,None)).reduceByKey(lambda x,y:None, numSplits, taskMemory, sort_shuffle)
        return g.map(lambda x_y1:x_y1[0])

    def top(self, n=10, key=None, reverse=False):
        if reverse:
            def topk(it):
                return heapq.nsmallest(n, it, key)
        else:
            def topk(it):
                return heapq.nlargest(n, it, key)
        return topk(sum(self.ctx.runJob(self, topk), []))

    def hot(self, n=10, numSplits=None, taskMemory=None, sort_shuffle=None):
        st = self.map(lambda x:(x,1)).reduceByKey(lambda x,y:x+y, numSplits, taskMemory, sort_shuffle=sort_shuffle)
        return st.top(n, key=lambda x:x[1])

    def fold(self, zero, f):
        '''Aggregate the elements of each partition, and then the
        results for all the partitions, using a given associative
        function and a neutral "zero value". The function op(t1, t2)
        is allowed to modify t1 and return it as its result value to
        avoid object allocation; however, it should not modify t2.'''
        return reduce(f,
                      self.ctx.runJob(self, lambda x: reduce(f, x, copy(zero))),
                      zero)

    def aggregate(self, zero, seqOp, combOp):
        '''Aggregate the elements of each partition, and then the
        results for all the partitions, using given combine functions
        and a neutral "zero value". This function can return a
        different result type, U, than the type of this RDD, T. Thus,
        we need one operation for merging a T into an U (seqOp(U, T))
        and one operation for merging two U's (combOp(U, U)). Both of
        these functions are allowed to modify and return their first
        argument instead of creating a new U to avoid memory
        allocation.'''
        return reduce(combOp,
                      self.ctx.runJob(self, lambda x: reduce(seqOp, x, copy(zero))),
                      zero)

    def count(self):
        return sum(self.ctx.runJob(self, lambda x: sum(1 for i in x)))

    def toList(self):
        return self.collect()

    def take(self, n):
        if n == 0: return []
        r = []
        p = 0
        while len(r) < n and p < len(self):
            res = list(self.ctx.runJob(self, lambda x: list(itertools.islice(x, n - len(r))), [p], True))[0]
            if res:
                r.extend(res)
            p += 1

        return r

    def first(self):
        r = self.take(1)
        if r: return r[0]

    def saveAsTextFile(self, path, ext='', overwrite=True, compress=False):
        return OutputTextFileRDD(self, path, ext, overwrite, compress=compress).collect()

    def saveAsTFRecordsFile(self, path, ext='', overwrite=True, compress=False):
        return OutputTfrecordstFileRDD(self, path, ext, overwrite, compress=compress).collect()

    def saveAsTextFileByKey(self, path, ext='', overwrite=True, compress=False):
        return MultiOutputTextFileRDD(self, path, ext, overwrite, compress=compress).collect()

    def saveAsCSVFile(self, path, dialect='excel', overwrite=True, compress=False):
        return OutputCSVFileRDD(self, path, dialect, overwrite, compress).collect()

    def saveAsBinaryFile(self, path, fmt, overwrite=True):
        return OutputBinaryFileRDD(self, path, fmt, overwrite).collect()

    def saveAsTableFile(self, path, overwrite=True):
        return OutputTableFileRDD(self, path, overwrite).collect()

    def saveAsBeansdb(self, path, depth=0, overwrite=True, compress=True,
                      raw=False, valueWithMeta=False):
        ''' save (key, value) pair in beansdb format files

        Args:
            depth: choice = [0, 1, 2].
                e.g. depth = 2 will write split N to 256 files:
                    'path/[0-F]/[0-F]/%03d.data' % N
                MUST use depth == 0 to generate data for rivendb
            raw: same as in DparkContext.beansdb
            valueWithMeta: expect TRIPLE as input value
        '''
        assert depth<=2, 'only support depth<=2 now'
        if len(self) >= 256:
            self = self.mergeSplit(len(self) // 256 + 1)
        return OutputBeansdbRDD(self, path, depth, overwrite, compress,
                                raw, valueWithMeta).collect()

    def saveAsTabular(self, path, field_names, **kw):
        from dpark.tabular import OutputTabularRDD
        return OutputTabularRDD(self, path, field_names, **kw).collect()

    # Extra functions for (K,V) pairs RDD
    def reduceByKeyToDriver(self, func):
        def mergeMaps(m1, m2):
            for k,v in six.iteritems(m2):
                m1[k]=func(m1[k], v) if k in m1 else v
            return m1
        return self.map(lambda x_y2:{x_y2[0]:x_y2[1]}).reduce(mergeMaps)

    def combineByKey(self, aggregator, splits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        if splits is None:
            splits = min(self.ctx.defaultMinSplits, len(self))
        if type(splits) is int:
            _thresh = None
            if fixSkew > 0 and splits > 1:
                _step = 100. / splits
                _offsets = [_step * i for i in range(1, splits)]
                _percentiles = self.percentiles(
                    _offsets, sampleRate=fixSkew, func=lambda t: portable_hash(t[0])
                )

                if _percentiles:
                    _thresh = []
                    for p in _percentiles:
                        if math.isnan(p):
                            continue

                        p = int(math.ceil(p))
                        if not _thresh or p > _thresh[-1]:
                            _thresh.append(p)

                    if len(_thresh) + 1 < splits:
                        logger.warning('Highly skewed dataset detected!')

                    splits = len(_thresh) + 1
                else:
                    _thresh = None

            splits = HashPartitioner(splits, thresholds=_thresh)
        return ShuffledRDD(self, aggregator, splits, taskMemory, sort_shuffle=sort_shuffle, iter_values=iter_values)

    def reduceByKey(self, func, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None):
        aggregator = Aggregator(lambda x:x, func, func)
        return self.combineByKey(aggregator, numSplits, taskMemory, fixSkew=fixSkew, sort_shuffle=sort_shuffle)

    def groupByKey(self, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        aggregator = GroupByAggregator()
        return self.combineByKey(aggregator, numSplits, taskMemory, fixSkew=fixSkew,
                                 sort_shuffle=sort_shuffle, iter_values=iter_values)

    def topByKey(self, top_n, order_func=None,
                 reverse=False, num_splits=None, task_memory=None, fixSkew=-1):
        ''' Base on groupByKey, return the top_n values in each group.
            The values in a key are ordered by a function object order_func.
            The result values in a key is order in inc order, if you want
            dec order, reverse is needed.
            We implement the top n function of values in heap. To keep stable in the input values,
            we trans the value in the records of rdd into a 4-tuple and sort them on heap.
            The values with same return under order function will be sorted by their order added to heap,
            only oldest value will be reserve.
            After call of combineByKey, we call the map to unpack the value from 4-tuple.
        :param top_n: required, limit of values to reserve in each group of a key
        :param order_func: optional, order function map a value to a comparable value, default None, when
        None, the value itself
        :param reverse: optional, bool, when True, the value is sorted in dec order, else inc order
        :param num_splits: same with groupByKey
        :param task_memory: same with groupByKey
        :return: rdd
        '''
        # To keep stable in heap, topByKey func need to generate the 4-tuple  which is
        # in the form of the
        # (order_func(v), partition id, sequence id, v), in the end of the combineByKey
        # return the v in the 4-th of the tuple
        def get_tuple_list(s_id, it, rev):
            return [(
                    i_k_v[1][0],
                    (order_func(i_k_v[1][1]) if order_func else i_k_v[1][1],
                     s_id if not rev else -s_id,
                     i_k_v[0] if not rev else -i_k_v[0],
                     i_k_v[1][1])
                ) for i_k_v in enumerate(it)]
        aggregator = HeapAggregator(top_n,
                                    order_reverse=reverse)
        rdd = EnumeratePartitionsRDD(
            self, lambda s_id, it: get_tuple_list(s_id, it, reverse)
        )
        return rdd.combineByKey(
                aggregator, num_splits, task_memory, fixSkew=fixSkew
            ) \
            .map(lambda x_ls: (x_ls[0], sorted(x_ls[1], reverse=reverse))) \
            .map(lambda k_ls: (k_ls[0], [x[-1] for x in k_ls[1]]))

    def partitionByKey(self, numSplits=None, taskMemory=None, sort_shuffle=None, iter_values=None):
        return self.groupByKey(numSplits, taskMemory, sort_shuffle=sort_shuffle, iter_values=iter_values).flatMapValue(lambda x: x)

    def update(self, other, replace_only=False, numSplits=None,
               taskMemory=None, fixSkew=-1,  sort_shuffle=None):
        rdd = self.mapValue(
            lambda val: (val, 1)  # bin('01') for old rdd
        ).union(
            other.mapValue(
                lambda val: (val, 2)  # bin('10') for new rdd
            )
        ).reduceByKey(
            lambda x, y: (y[0] if y[1] > x[1] else x[0], x[1] | y[1]),
            numSplits,
            taskMemory,
            fixSkew=fixSkew,
            sort_shuffle=sort_shuffle
        )
        # rev:
        #   1(01): old value
        #   2(10): new added value
        #   3(11): new updated value
        if replace_only:
            rdd = rdd.filter(
                lambda key_val_rev: key_val_rev[1][1] != 2
            )
        return rdd.mapValue(
            lambda val_rev: val_rev[0]
        )

    def innerJoin(self, smallRdd):
        """
        This is functionally equivalent to `join`, but `innerJoin` assume `smallRdd` is a
        small Data set, and `innerJoin` will broadcast the `smallRdd` to optimize running time.

        >>> x = dpark.parallelize([("a", 1), ("b", 4)])
        >>> y = dpark.parallelize([("a", 2), ("a", 3)])
        >>> x.innerJoin(y).collect()
        [('a', (1, 2)), ('a', (1, 3))]
        """
        o = collections.defaultdict(list)
        for (k, v) in smallRdd:
            o[k].append(v)
        o_b = self.ctx.broadcast(o)
        def do_join(k_v):
            (k, v) = k_v
            for v1 in o_b.value[k]:
                yield (k, (v, v1))
        r = self.flatMap(do_join)
        r.mem += (o_b.bytes * 10) >> 20 # memory used by broadcast obj
        return r

    def join(self, other, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        return self._join(other, (), numSplits, taskMemory, fixSkew=fixSkew, sort_shuffle=sort_shuffle,iter_values=iter_values)

    def leftOuterJoin(self, other, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        return self._join(other, (1,), numSplits, taskMemory, fixSkew=fixSkew, sort_shuffle=sort_shuffle, iter_values=iter_values)

    def rightOuterJoin(self, other, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        return self._join(other, (2,), numSplits, taskMemory, fixSkew=fixSkew, sort_shuffle=sort_shuffle, iter_values=iter_values)

    def outerJoin(self, other, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        return self._join(other, (1,2), numSplits, taskMemory, fixSkew=fixSkew, sort_shuffle=sort_shuffle, iter_values=iter_values)

    def _join(self, other, keeps, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):

        def dispatch(k_seq):
            (k, seq) = k_seq
            vbuf, wbuf = seq
            if not isinstance(wbuf, (list, tuple)):
                wbuf = list(wbuf)
            if not vbuf and 2 in keeps:
                vbuf.append(None)
            if not wbuf and 1 in keeps:
                wbuf.append(None)
            for vv in vbuf:
                for ww in wbuf:
                    yield (k, (vv, ww))

        return self.cogroup(other, numSplits, taskMemory, fixSkew=fixSkew, sort_shuffle=sort_shuffle, iter_values=iter_values) \
            .flatMap(dispatch)

    def collectAsMap(self):
        d = {}
        for v in self.ctx.runJob(self, lambda x: list(x)):
            d.update(dict(v))
        return d

    def mapValue(self, f):
        return MappedValuesRDD(self, f)

    def flatMapValue(self, f):
        return FlatMappedValuesRDD(self, f)

    def groupWith(self, others, numSplits=None, taskMemory=None, fixSkew=-1, sort_shuffle=None, iter_values=None):
        if isinstance(others, RDD):
            others = [others]

        _numSplits = numSplits
        if _numSplits is None:
            if self.partitioner is not None:
                _numSplits = self.partitioner.numPartitions
            else:
                _numSplits = self.ctx.defaultParallelism

        _thresh = None
        if fixSkew > 0 and _numSplits > 1:
            _step = 100. / _numSplits
            _offsets = [_step * i for i in range(1, _numSplits)]
            _percentiles = self.union(*others) \
                .percentiles(
                    _offsets, sampleRate=fixSkew, func=lambda t: portable_hash(t[0])
                )

            if _percentiles:
                _thresh = []
                for p in _percentiles:
                    if math.isnan(p):
                        continue

                    p = int(math.ceil(p))
                    if not _thresh or p > _thresh[-1]:
                        _thresh.append(p)

                if len(_thresh) + 1 < _numSplits:
                    logger.warning('Highly skewed dataset detected!')

                _numSplits = len(_thresh) + 1
            else:
                _thresh = None

        part = HashPartitioner(_numSplits, thresholds=_thresh)
        rdd = CoGroupedRDD([self]+others, part, taskMemory, sort_shuffle=sort_shuffle, iter_values=iter_values)
        return rdd

    cogroup = groupWith

    def lookup(self, key):
        if self.partitioner:
            index = self.partitioner.getPartition(key)
            def process(it):
                for k,v in it:
                    if k == key:
                        return v
            result = list(self.ctx.runJob(self, process, [index], False))
            return result[0] if result else None
        else:
            logger.warning("Too much time may be taken to lookup in a RDD without a partitioner!")
            result = self.flatMap(lambda k_v:[k_v[1]] if k_v[0]==key else []).take(1)
            return result[0] if result else None

    def asTable(self, fields, name=''):
        from dpark.table import TableRDD
        return TableRDD(self, fields, name)

    def batch(self, size):
        def _batch(iterable):
            sourceiter = iter(iterable)
            while True:
                s = list(itertools.islice(sourceiter, size))
                if s:
                    yield s
                else:
                    return

        return self.glom().flatMap(_batch)

    def adcount(self):
        "approximate distinct counting"
        r = self.map(lambda x:(1, x)).adcountByKey(1).collectAsMap()
        return r and r[1] or 0

    def adcountByKey(self, splits=None, taskMemory=None, fixSkew=-1):
        try:
            from pyhll import HyperLogLog
        except ImportError:
            from .hyperloglog import HyperLogLog
        def create(v):
            return HyperLogLog([v], 16)
        def combine(s, v):
            return s.add(v) or s
        def merge(s1, s2):
            return s1.update(s2) or s1
        agg = Aggregator(create, combine, merge)
        return self.combineByKey(agg, splits, taskMemory, fixSkew=fixSkew) \
            .mapValue(len)

    def percentiles(self, p, sampleRate=1.0, func=None):
        def _(it):
            from dpark.tdigest import TDigest
            digest = TDigest()
            for k in it:
                digest.add(k)

            digest.compress()
            yield digest

        if sampleRate <= 0:
            raise ValueError('Sample Rate should be positive.')

        if sampleRate >= 1.0:
            rdd = self
        else:
            rdd = self.sample(sampleRate)

        if func:
            rdd = rdd.map(func)

        _digest = rdd.mapPartitions(_).reduce(lambda x, y: x + y)
        _digest.compress()
        return [_digest.quantile(pp / 100.) for pp in p]

    def percentilesByKey(self, p, sampleRate=1.0, func=None,
                         numSplits=None, taskMemory=None, fixSkew=-1):
        def _create(x):
            from dpark.tdigest import TDigest
            digest = TDigest()
            digest.add(x)
            return digest

        def _update(d, x):
            d.add(x)
            return d

        def _merge(d1, d2):
            d = d1 + d2
            d.compress()
            return d

        def _(d):
            return [d.quantile(pp / 100.) for pp in p]

        if sampleRate <= 0:
            raise ValueError('Sample Rate should be positive.')

        if sampleRate >= 1.0:
            rdd = self
        else:
            rdd = self.sample(sampleRate)

        if func:
            rdd = rdd.mapValue(func)

        aggregator = Aggregator(_create, _update, _merge)
        return rdd.combineByKey(aggregator, numSplits, taskMemory, fixSkew=fixSkew) \
            .mapValue(_)


    def with_cpus(self, cpus):
        self.cpus = cpus
        return self

    def with_gpus(self, gpus):
        self.gpus = gpus
        return self

    def with_mem(self, mem):
        self.mem = mem
        return self


class DerivedRDD(RDD):
    def __init__(self, rdd):
        RDD.__init__(self, rdd.ctx)
        self.prev = rdd
        self.mem = max(self.mem, rdd.mem)
        self.cpus = rdd.cpus
        self.gpus = rdd.gpus
        self._dependencies = [OneToOneDependency(rdd)]
        self._splits = self.prev.splits
        self._preferred_locs = self.prev._preferred_locs
        self.repr_name = '<%s %s>' % (self.__class__.__name__, rdd)

    def _clear_dependencies(self):
        RDD._clear_dependencies(self)
        self.prev = None

    @property
    def splits(self):
        if self._checkpoint_rdd:
            return self._checkpoint_rdd.splits
        return self._splits


class MappedRDD(DerivedRDD):
    def __init__(self, prev, func=lambda x:x):
        DerivedRDD.__init__(self, prev)
        self.func = func

    def compute(self, split):
        if self.err < 1e-8:
            return (self.func(v) for v in self.prev.iterator(split))
        return self._compute_with_error(split)

    def _compute_with_error(self, split):
        total, err = 0, 0
        for v in self.prev.iterator(split):
            try:
                total += 1
                yield self.func(v)
            except Exception as e:
                logger.warning("ignored record %r: %s", v, e)
                err += 1
                if total > 100 and err > total * self.err * 10:
                    raise Exception("too many error occured: %s" % (float(err)/total))

        if err > total * self.err:
            raise Exception("too many error occured: %s" % (float(err)/total))

    @cached
    def __getstate__(self):
        d = RDD.__getstate__(self)
        del d['func']
        return d, dump_func(self.func)

    def __setstate__(self, state):
        self.__dict__, code = state
        try:
            self.func = load_func(code)
        except Exception:
            raise

class FlatMappedRDD(MappedRDD):
    def compute(self, split):
        if self.err < 1e-8:
            return chain(self.func(v) for v in self.prev.iterator(split))
        return self._compute_with_error(split)

    def _compute_with_error(self, split):
        total, err = 0, 0
        for v in self.prev.iterator(split):
            try:
                total += 1
                for k in self.func(v):
                    yield k
            except Exception as e:
                logger.warning("ignored record %r: %s", v, e)
                err += 1
                if total > 100 and err > total * self.err * 10:
                    raise Exception("too many error occured: %s, %s" % ((float(err)/total), e))

        if err > total * self.err:
            raise Exception("too many error occured: %s, %s" % ((float(err)/total), e))


class FilteredRDD(MappedRDD):
    def compute(self, split):
        if self.err < 1e-8:
            return (v for v in self.prev.iterator(split) if self.func(v))
        return self._compute_with_error(split)

    def _compute_with_error(self, split):
        total, err = 0, 0
        for v in self.prev.iterator(split):
            try:
                total += 1
                if self.func(v):
                    yield v
            except Exception as e:
                logger.warning("ignored record %r: %s", v, e)
                err += 1
                if total > 100 and err > total * self.err * 10:
                    raise Exception("too many error occured: %s" % (float(err)/total))

        if err > total * self.err:
            raise Exception("too many error occured: %s" % (float(err)/total))

class GlommedRDD(DerivedRDD):
    def compute(self, split):
        yield list(self.prev.iterator(split))

class MapPartitionsRDD(MappedRDD):
    def compute(self, split):
        return self.func(self.prev.iterator(split))

class EnumeratePartitionsRDD(MappedRDD):
    def compute(self, split):
        return self.func(split.index, self.prev.iterator(split))

class PipedRDD(DerivedRDD):
    def __init__(self, prev, command, quiet=False, shell=False):
        DerivedRDD.__init__(self, prev)
        self.command = command
        self.quiet = quiet
        self.shell = shell
        self.repr_name = '<PipedRDD %s %s>' % (' '.join(command), prev)

    def compute(self, split):
        import subprocess
        devnull = open(os.devnull, 'w')
        p = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.quiet and devnull or sys.stderr,
                shell=self.shell)

        def read(stdin):
            try:
                it = iter(self.prev.iterator(split))
                # fetch the first item
                for first in it:
                    break
                else:
                    return
                try:
                    if isinstance(first, six.binary_type) and first.endswith(b'\n'):
                        stdin.write(first)
                        stdin.writelines(it)
                    else:
                        stdin.write(("%s\n"%first).encode('utf-8'))
                        stdin.writelines(("%s\n"%x).encode('utf-8') for x in it)
                except Exception as e:
                    if not (isinstance(e, IOError) and e.errno == 32): # Broken Pipe
                        self.error = e
                        p.kill()
            finally:
                stdin.close()
                devnull.close()

        self.error = None
        spawn(read, p.stdin)
        return self.read(p)

    def read(self, p):
        for line in p.stdout:
            yield line[:-1]
        if self.error:
            raise self.error
        ret = p.wait()
        p.stdout.close()
        #if ret:
        #    raise Exception('Subprocess exited with status %d' % ret)

class MappedValuesRDD(MappedRDD):
    @property
    def partitioner(self):
        return self.prev.partitioner

    def compute(self, split):
        func = self.func
        if self.err < 1e-8:
            return ((k,func(v)) for k,v in self.prev.iterator(split))
        return self._compute_with_error(split)

    def _compute_with_error(self, split):
        func = self.func
        total, err = 0, 0
        for k,v in self.prev.iterator(split):
            try:
                total += 1
                yield (k,func(v))
            except Exception as e:
                logger.warning("ignored record %r: %s", v, e)
                err += 1
                if total > 100 and err > total * self.err * 10:
                    raise Exception("too many error occured: %s" % (float(err)/total))

        if err > total * self.err:
            raise Exception("too many error occured: %s" % (float(err)/total))

class FlatMappedValuesRDD(MappedValuesRDD):
    def compute(self, split):
        total, err = 0, 0
        for k,v in self.prev.iterator(split):
            try:
                total += 1
                for vv in self.func(v):
                    yield k,vv
            except Exception as e:
                logger.warning("ignored record %r: %s", v, e)
                err += 1
                if total > 100 and err > total * self.err * 10:
                    raise Exception("too many error occured: %s" % (float(err)/total))

        if err > total * self.err:
            raise Exception("too many error occured: %s" % (float(err)/total))

class ShuffledRDDSplit(Split):
    def __hash__(self):
        return self.index

class ShuffledRDD(RDD):
    def __init__(self, parent, aggregator, part, taskMemory=None, sort_shuffle=None, iter_values=None):
        RDD.__init__(self, parent.ctx)
        self.numParts = len(parent)
        self.aggregator = aggregator
        self.sort_shuffle = sort_shuffle if sort_shuffle is not None else self.ctx.options.sort_shuffle
        if iter_values and not isinstance(self.aggregator, GroupByAggregator):
            logger.warning("iter_values only work for groupBy, groupWith and join")
            self.iter_values = False
        else:
             self.iter_values = iter_values if iter_values is not None else self.ctx.options.iter_values

        self._partitioner = part
        if taskMemory:
            self.mem = taskMemory
        self._splits = [ShuffledRDDSplit(i) for i in range(part.numPartitions)]
        self.shuffleId = self.ctx.newShuffleId()
        self._dependencies = [ShuffleDependency(self.shuffleId,
                parent, aggregator, part, sort_shuffle=self.sort_shuffle, iter_values=iter_values)]
        self.repr_name = '<ShuffledRDD %s>' % parent

    @cached
    def __getstate__(self):
        d = RDD.__getstate__(self)
        return d

    def compute(self, split):
        if not self.sort_shuffle:
            if isinstance(self.aggregator, GroupByAggregator):
                merger = OrderedMerger(self.aggregator)
            else:
                merger = Merger(self.aggregator)
            fetcher = env.shuffleFetcher
            fetcher.fetch(self.shuffleId, split.index, merger.merge)
        else:
            fetcher = SortedShuffleFetcher()
            iters = fetcher.get_iters(self.shuffleId, split.index)
            if self.iter_values:
                merger = SortedGroupMerger(self.scope.call_site)
            else:
                merger = SortedMerger(self.aggregator)
            merger.merge(iters)

        return merger


DEFAULT_CACHE_MEMORY_SIZE = 256


class CartesianSplit(Split):
    def __init__(self, idx, s1, s2):
        self.index = idx
        self.s1 = s1
        self.s2 = s2


class CartesianRDD(RDD):
    def __init__(self, rdd1, rdd2, taskMemory=None, cacheMemory=DEFAULT_CACHE_MEMORY_SIZE):
        RDD.__init__(self, rdd1.ctx)
        self.rdd1 = rdd1
        self.rdd2 = rdd2
        self.cache_memory = int(max(
            DEFAULT_CACHE_MEMORY_SIZE,
            cacheMemory or DEFAULT_CACHE_MEMORY_SIZE
        ))
        self.mem = int(max(
            taskMemory or self.ctx.options.mem,
            rdd1.mem * 1.5,
            rdd2.mem * 1.5,
            self.cache_memory * 2.5
        ))
        self.cpus = max(
            rdd1.cpus,
            rdd2.cpus
        )
        self.gpus = int(max(
            rdd1.gpus,
            rdd2.gpus
        ))

        self.numSplitsInRdd2 = n = len(rdd2)
        self._splits = [CartesianSplit(s1.index*n+s2.index, s1, s2)
            for s1 in rdd1.splits for s2 in rdd2.splits]
        self._dependencies = [CartesianDependency(rdd1, True, n),
                             CartesianDependency(rdd2, False, n)]
        self._preferred_locs = {}
        for split in self._splits:
            self._preferred_locs[split] = rdd1.preferredLocations(split.s1) + rdd2.preferredLocations(split.s2)

        self.repr_name = '<Cartesian %s and %s>' % (self.rdd1, self.rdd2)

    def _clear_dependencies(self):
        RDD._clear_dependencies(self)
        self.rdd1 = self.rdd2 = None

    def compute(self, split):
        saved = False
        _cached = None
        basedir = os.path.join(env.workdir[-1], 'temp')
        mkdir_p(basedir)
        with tempfile.SpooledTemporaryFile(self.cache_memory << 20, dir=basedir) as f:
            for i in self.rdd1.iterator(split.s1):
                if not saved:
                    with gzip.GzipFile('dummy', fileobj=f, compresslevel=1) as gf:
                        pickler = Pickler(gf, -1)
                        for j in self.rdd2.iterator(split.s2):
                            yield i, j
                            pickler.dump(j)

                    saved = True

                elif _cached is not None:
                    # speedup when memory is enough
                    for j in _cached:
                        yield i, j

                else:
                    if not f._rolled:
                        _cached = []

                    f.seek(0)
                    with gzip.GzipFile(fileobj=f, mode='rb') as gf:
                        unpickler = Unpickler(gf)
                        try:
                            while True:
                                j = unpickler.load()
                                yield i, j
                                if _cached is not None:
                                    _cached.append(j)
                        except EOFError:
                            pass

                    if _cached is not None:
                        f.close()


class CoGroupSplitDep: pass
class NarrowCoGroupSplitDep(CoGroupSplitDep):
    def __init__(self, rdd, split):
        self.rdd = rdd
        self.split = split
class ShuffleCoGroupSplitDep(CoGroupSplitDep):
    def __init__(self, shuffleId):
        self.shuffleId = shuffleId

class CoGroupSplit(Split):
    def __init__(self, idx, deps):
        self.index = idx
        self.deps = deps
    def __hash__(self):
        return self.index


class CoGroupedRDD(RDD):
    def __init__(self, rdds, partitioner, taskMemory=None, sort_shuffle=None, iter_values=None):
        RDD.__init__(self, rdds[0].ctx)
        self.size = len(rdds)
        if taskMemory:
            self.mem = taskMemory
        self.aggregator = GroupByAggregator()
        self._partitioner = partitioner
        self.sort_shuffle = sort_shuffle if sort_shuffle is not None else self.ctx.options.sort_shuffle
        self.iter_values = iter_values if iter_values is not None else self.ctx.options.iter_values
        self._dependencies = dep = [rdd.partitioner == partitioner
                and OneToOneDependency(rdd)
                or ShuffleDependency(self.ctx.newShuffleId(),
                    rdd, self.aggregator, partitioner, sort_shuffle=self.sort_shuffle, iter_values=iter_values)
                for i,rdd in enumerate(rdds)]
        self._splits = [CoGroupSplit(j,
                          [isinstance(dep[i],ShuffleDependency)
                            and ShuffleCoGroupSplitDep(dep[i].shuffleId)
                            or NarrowCoGroupSplitDep(r, r.splits[j])
                            for i,r in enumerate(rdds)])
                        for j in range(partitioner.numPartitions)]
        self.repr_name = ('<CoGrouped of %s>' % (','.join(str(rdd) for rdd in rdds)))[:80]
        self._preferred_locs = {}
        for split in self._splits:
            self._preferred_locs[split] = sum([dep.rdd.preferredLocations(dep.split) for dep in split.deps
                                               if isinstance(dep, NarrowCoGroupSplitDep)], [])

    def _compute_hash_merge(self, split):
        m = OrderedCoGroupMerger(self.size)
        for i, dep in enumerate(split.deps):
            if isinstance(dep, NarrowCoGroupSplitDep):
                m.append(i, dep.rdd.iterator(dep.split))
            elif isinstance(dep, ShuffleCoGroupSplitDep):
                def merge(items, map_id):
                    m.extend(i, items, map_id)
                env.shuffleFetcher.fetch(dep.shuffleId, split.index, merge)
        return m

    def _compute_sort_merge(self, split):

        def _enum_value(items, n):
            for k, v in items:
                yield k, (n, v)

        iters = []
        fetcher = SortedShuffleFetcher()
        for i, dep in enumerate(split.deps):
            if isinstance(dep, NarrowCoGroupSplitDep):
                it = sorted(dep.rdd.iterator(dep.split), key=lambda x: x[0])
                it = self.aggregator.aggregate_sorted(it)
                it = _enum_value(it, i)
                iters.append(it)
            elif isinstance(dep, ShuffleCoGroupSplitDep):
                its = fetcher.get_iters(dep.shuffleId, split.index)
                iters.extend([_enum_value(it, i) for it in its])
        merger = CoGroupSortedMerger(self.size)
        merger.merge(iters)
        return merger

    def _compute_sort_merge_iter(self, split):
        iters = []
        fetcher = SortedShuffleFetcher()
        for i, dep in enumerate(split.deps):
            if isinstance(dep, NarrowCoGroupSplitDep):
                it = sorted(dep.rdd.iterator(dep.split), key=lambda x: x[0])
                it = self.aggregator.aggregate_sorted(it)
                iters.append(it)
            elif isinstance(dep, ShuffleCoGroupSplitDep):
                its = fetcher.get_iters(dep.shuffleId, split.index)
                merger = SortedGroupMerger(self.scope.call_site)
                merger.merge(its)
                iters.append(merger)

        merger = StreamCoGroupSortedMerger()
        merger.merge(iters)
        return merger

    def compute(self, split):
        if not self.sort_shuffle:
            return self._compute_hash_merge(split)
        elif self.iter_values:
            return self._compute_sort_merge_iter(split)
        else:
            return self._compute_sort_merge(split)


class SampleRDD(DerivedRDD):
    def __init__(self, prev, frac, withReplacement, seed):
        DerivedRDD.__init__(self, prev)
        self.frac = frac
        self.withReplacement = withReplacement
        self.seed = seed
        self.repr_name = '<SampleRDD(%s) of %s>' % (frac, prev)

    def compute(self, split):
        rd = random.Random(self.seed + split.index)
        if self.withReplacement:
            olddata = list(self.prev.iterator(split))
            sampleSize = int(math.ceil(len(olddata) * self.frac))
            for i in range(sampleSize):
                yield rd.choice(olddata)
        else:
            for i in self.prev.iterator(split):
                if rd.random() <= self.frac:
                    yield i


class UnionSplit(Split):
    def __init__(self, idx, rdd, split):
        self.index = idx
        self.rdd = rdd
        self.split = split

class UnionRDD(RDD):
    def __init__(self, ctx, rdds):
        RDD.__init__(self, ctx)
        if rdds:
            self.mem = max(rdd.mem for rdd in rdds)
            self.cpus = max(rdd.cpus for rdd in rdds)
            self.gpus = max(rdd.cpus for rdd in rdds)

        pos = 0
        for rdd in rdds:
            self._splits.extend([UnionSplit(pos + i, rdd, sp) for i, sp in enumerate(rdd.splits)])
            self._dependencies.append(RangeDependency(rdd, 0, pos, len(rdd)))
            pos += len(rdd)
        self.repr_name = '<UnionRDD %d %s ...>' % (len(rdds), ','.join(str(rdd) for rdd in rdds[:1]))
        self._preferred_locs = {}
        for split in self._splits:
            self._preferred_locs[split] = split.rdd.preferredLocations(split.split)

    def compute(self, split):
        return split.rdd.iterator(split.split)

class SliceRDD(RDD):
    def __init__(self, rdd, i, j):
        RDD.__init__(self, rdd.ctx)
        self.rdd = rdd
        self.mem = rdd.mem
        self.cpus = rdd.cpus
        self.gpus = rdd.gpus
        if j > len(rdd):
            j = len(rdd)
        self.i = i
        self.j = j
        self._splits = rdd.splits[i:j]
        self._dependencies = [RangeDependency(rdd, i, 0, j-i)]
        self._preferred_locs = {}
        for split in self._splits:
            self._preferred_locs[split] = rdd.preferredLocations(split)

        self.repr_name = '<SliceRDD [%d:%d] of %s>' % (i, j, rdd)

    def _clear_dependencies(self):
        RDD._clear_dependencies(self)
        self.rdd = None

    def compute(self, split):
        return self.rdd.iterator(split)


class MultiSplit(Split):
    def __init__(self, index, splits):
        self.index = index
        self.splits = splits

class MergedRDD(RDD):
    def __init__(self, rdd, splitSize=None, numSplits=None):
        RDD.__init__(self, rdd.ctx)
        if splitSize is None:
            splitSize = (len(rdd) + numSplits - 1) // numSplits
        numSplits = (len(rdd) + splitSize - 1) // splitSize
        self.rdd = rdd
        self.mem = rdd.mem
        self.cpus = rdd.cpus
        self.gpus = rdd.gpus
        self.splitSize = splitSize
        self.numSplits = numSplits

        splits = rdd.splits
        self._splits = [MultiSplit(i, splits[i*splitSize:(i+1)*splitSize])
               for i in range(numSplits)]
        self._dependencies = [OneToRangeDependency(rdd, splitSize, len(rdd))]
        self._preferred_locs = {}
        for split in self._splits:
            self._preferred_locs[split] = sum([rdd.preferredLocations(sp) for sp in split.splits], [])

        self.repr_name = '<MergedRDD %s:1 of %s>' % (splitSize, rdd)

    def _clear_dependencies(self):
        RDD._clear_dependencies(self)
        self.rdd = None

    def compute(self, split):
        return chain(self.rdd.iterator(sp) for sp in split.splits)


class ZippedRDD(RDD):
    def __init__(self, ctx, rdds):
        assert len(set([len(rdd) for rdd in rdds])) == 1, 'rdds must have the same length'
        RDD.__init__(self, ctx)
        self.rdds = rdds
        self.mem = max(r.mem for r in rdds)
        self.cpus = max(r.cpus for r in rdds)
        self.gpus = max(r.gpus for r in rdds)
        self._splits = [MultiSplit(i, splits)
                for i, splits in enumerate(zip(*[rdd.splits for rdd in rdds]))]
        self._dependencies = [OneToOneDependency(rdd) for rdd in rdds]
        self._preferred_locs = {}
        for split in self._splits:
            self._preferred_locs[split] = sum(
                [rdd.preferredLocations(sp) for rdd,sp in zip(self.rdds, split.splits)], [])
        self.repr_name = '<Zipped %s>' % (','.join(str(rdd) for rdd in rdds))

    def _clear_dependencies(self):
        RDD._clear_dependencies(self)
        self.rdds = []

    def compute(self, split):
        return zip(
            *[rdd.iterator(sp) for rdd, sp in zip(self.rdds, split.splits)]
        )


class CSVReaderRDD(DerivedRDD):
    def __init__(self, prev, dialect='excel'):
        DerivedRDD.__init__(self, prev)
        self.dialect = dialect
        self.repr_name = '<CSVReaderRDD %s of %s>' % (dialect, prev)

    def compute(self, split):
        return csv.reader(self.prev.iterator(split), self.dialect)


class ParallelCollectionSplit:
    def __init__(self, ctx, index, values):
        self.index = index
        _values = six.moves.cPickle.dumps(values, -1)
        length = len(_values)
        data_limit = ctx.data_limit
        if data_limit is None or length < data_limit:
            self.values = _values
            self.is_broadcast = False
        else:
            self.values = ctx.broadcast(_values)
            self.is_broadcast = True


class ParallelCollection(RDD):
    def __init__(self, ctx, data, numSlices, taskMemory=None):
        RDD.__init__(self, ctx)
        self.size = len(data)
        if taskMemory:
            self.mem = taskMemory
        slices = self.slice(data, max(1, min(self.size, numSlices)))
        self._splits = [ParallelCollectionSplit(ctx, i, slices[i])
                for i in range(len(slices))]
        self._dependencies = []
        self.repr_name = '<ParallelCollection %d>' % self.size

    def compute(self, split):
        if split.is_broadcast:
            _values = split.values.value
        else:
            _values =split.values

        return six.moves.cPickle.loads(_values)

    @classmethod
    def slice(cls, data, numSlices):
        if numSlices <= 0:
            raise ValueError("invalid numSlices %d" % numSlices)
        m = len(data)
        if not m:
            return [[]]
        n = m // numSlices
        if m % numSlices != 0:
            n += 1
        if isinstance(data, range):
            first = data[0]
            last = data[m-1]
            step = (last - first) // (m-1)
            nstep = step * n
            slices = [range(first+i*nstep, first+(i+1)*nstep, step)
                for i in range(numSlices-1)]
            slices.append(range(first+(numSlices-1)*nstep,
                min(last+step, first+numSlices*nstep), step))
            return slices
        if not isinstance(data, list):
            data = list(data)
        return [data[i*n : i*n+n] for i in range(numSlices)]


class CheckpointRDD(RDD):
    def __init__(self, ctx, path):
        RDD.__init__(self, ctx)
        self.path = path
        files = self.generated_files(path)
        if not files or files[0] != '0' or files[-1] != str(len(files)-1):
            raise RuntimeError('Invalid checkpoint directory: %s' % path)

        self.files = files
        self._splits = [Split(i) for i in range(len(files))]

    @classmethod
    def generated_files(cls, path):
        return sorted(filter(str.isdigit, os.listdir(path)), key=int)

    def compute(self, split):
        try:
            with open(os.path.join(self.path, self.files[split.index]), 'rb') as f:
                return six.moves.cPickle.loads(f.read())
        except IOError:
            time.sleep(1)
            with open(os.path.join(self.path, self.files[split.index]), 'rb') as f:
                return six.moves.cPickle.loads(f.read())


class PartialSplit(Split):
    def __init__(self, index, begin, end):
        self.index = index
        self.begin = begin
        self.end = end


class TextFileRDD(RDD):

    DEFAULT_SPLIT_SIZE = 64*1024*1024

    def __init__(self, ctx, path, numSplits=None, splitSize=None):
        RDD.__init__(self, ctx)
        self.path = path
        with closing(open_file(path)) as file_:
            self.size = size = file_.length

            if splitSize is None:
                if numSplits is None:
                    splitSize = self.DEFAULT_SPLIT_SIZE
                else:
                    splitSize = size // numSplits or self.DEFAULT_SPLIT_SIZE
            numSplits = size // splitSize
            if size % splitSize > 0:
                numSplits += 1
            self.splitSize = splitSize
            self._splits = [PartialSplit(i, i*splitSize, min(size, (i+1) * splitSize))
                        for i in range(numSplits)]

            self._preferred_locs = {}
            for split in self._splits:
                if self.splitSize != CHUNKSIZE:
                    start = split.begin // CHUNKSIZE
                    end = (split.end + CHUNKSIZE - 1) // CHUNKSIZE
                    self._preferred_locs[split] = sum((file_.locs(i) for i in range(start, end)), [])
                else:
                    self._preferred_locs[split] = file_.locs(split.begin // self.splitSize)
                hostnames = []
                for loc in self._preferred_locs[split]:
                    host = loc
                    try:
                        host = socket.gethostbyaddr(loc)[0]
                    except IOError as e:
                        logger.warning('get hostname exec %s for loc %s', e.message, loc)
                    hostnames.append(host)
                self._preferred_locs[split] = hostnames
        self.repr_name = '<%s %s>' % (self.__class__.__name__, path)

    def open_file(self):
        return open_file(self.path)

    def compute(self, split):
        with closing(self.open_file()) as f:
            start = split.begin
            end = split.end
            if start > 0:
                f.seek(start-1)
                byte = f.read(1)
                while byte != b'\n':
                    byte = f.read(1)
                    if not byte:
                        return
                    start += 1

            if start >= end:
                return

            for l in self.read(f, start, end):
                yield l

    def read(self, f, start, end):
        for line in f:
            size = len(line)
            if not six.PY2:
                line = line.decode('utf-8')

            if line.endswith('\n'):
                yield line[:-1]
            else:
                yield line
            start += size
            if start >= end: break

class TfrecordsRDD(TextFileRDD):

    DEFAULT_READ_SIZE = 1 << 10
    BLOCK_SIZE = 64 << 10

    def __init__(self, ctx, path, numSplits=None, splitSize=None):
        TextFileRDD.__init__(self, ctx, path, numSplits, splitSize)

    def compute(self, split):
        with closing(self.open_file()) as f:
            if self.path.endswith('.gz'):
                # compute the start & end boundary of gzip file (cut block)
                if split.index == 0:
                    zf = gzip.GzipFile(mode='rb', fileobj=f)
                    if hasattr(zf, '_buffer'):
                        zf._buffer.raw._read_gzip_header()
                    else:
                        zf._read_gzip_header()
                    zf.close()
                    start = f.tell()
                else:
                    start = gzip_find_block(f, split.index * self.splitSize)
                    if start >= split.index * self.splitSize + self.splitSize:
                        return
                end = gzip_find_block(f, split.index * self.splitSize + self.splitSize)

                f.seek(start)
                f.length = end
                cross_record = BytesIO()
                for fh in gzip_decompressed_fh_2(f, self.path):
                    point = self.check_block_split_point(fh)
                    fh.seek(0)      # speed up
                    if point is None:
                        cross_record.write(fh.read())
                    else:
                        cross_record.write(fh.read(point))
                        cross_record.seek(0)
                        for rcd in self.compute_with_fh(cross_record, 0, float('inf')):
                            yield rcd
                        cross_record.seek(0)    # speed up
                        cross_record.truncate()     # clear the buffer
                        cross_record.write(fh.read())   # write next record head
                        if f.tell() > end:
                            break
            else:
                start = split.begin
                end = split.end
                for rcd in self.compute_with_fh(f, start, end):
                    yield rcd

    def compute_with_fh(self, f, start, end):
        if start >= 0:
            f.seek(start)
            buffer = f.read(min(self.DEFAULT_READ_SIZE, end - f.tell()))
            while start < end:
                cursor = 0
                while cursor < len(buffer) - 11 and not self.check_split_point(buffer[cursor:cursor + 12]):
                    cursor += 1
                start += cursor
                if cursor == len(buffer) - 11:
                    start += 11
                    buffer = buffer[-11:] + f.read(min(self.DEFAULT_READ_SIZE, end - f.tell()))
                else:
                    break
        if start >= end:
            return
        f.seek(start)
        while start < end:
            record = self.get_single_record(f)
            if record is None:
                return
            yield record
            start += len(record) + 16

    def check_block_split_point(self, f):
        buffer = f.read()   # speed up
        cursor = 0
        while cursor < len(buffer) - 11 and not self.check_split_point(buffer[cursor:cursor + 12]):
            cursor += 1
        if cursor == len(buffer) - 11:
            return None
        else:
            return cursor

    def check_split_point(self, buf):
        buf_length_expected = 12
        if not buf:
            return False
        if len(buf) != buf_length_expected:
            return False
        length, length_mask_expected = struct.unpack('<QI', buf)
        length_mask_actual = masked_crc32c(buf[:8])
        return length_mask_actual == length_mask_expected

    def get_single_record(self, f):
        buf_length_expected = 12
        buf = f.read(buf_length_expected)
        if not buf:
            return None
        if len(buf) != buf_length_expected:
            raise ValueError('Not a valid TFRecord. Fewer than %d bytes: %s' % (buf_length_expected, buf))
        length, length_mask_expected = struct.unpack('<QI', buf)
        length_mask_actual = masked_crc32c(buf[:8])
        if length_mask_actual == length_mask_expected:
            # data verification
            buf_length_expected = length + 4
            buf = f.read(buf_length_expected)
            if len(buf) != buf_length_expected:
                raise ValueError('Not a valid TFRecord. Fewer than %d bytes: %s' % (buf_length_expected, buf))
            data, data_mask_expected = struct.unpack('<%dsI' % length, buf)
            data_mask_actual = masked_crc32c(data)
            if data_mask_actual == data_mask_expected:
                return data.decode()
            else:
                logger.error("data loss!!!")  # Note: Pending
        else:
            return None

class PartialTextFileRDD(TextFileRDD):
    def __init__(self, ctx, path, firstPos, lastPos, splitSize=None, numSplits=None):
        RDD.__init__(self, ctx)
        self.path = path
        self.firstPos = firstPos
        self.lastPos = lastPos
        self.size = size = lastPos - firstPos

        if splitSize is None:
            if numSplits is None:
                splitSize = self.DEFAULT_SPLIT_SIZE
            else:
                splitSize = size // numSplits or self.DEFAULT_SPLIT_SIZE
        self.splitSize = splitSize
        if size <= splitSize:
            self._splits = [PartialSplit(0, firstPos, lastPos)]
        else:
            first_edge = firstPos // splitSize * splitSize + splitSize
            last_edge = (lastPos-1) // splitSize * splitSize
            ns = (last_edge - first_edge) // splitSize
            self._splits = [PartialSplit(0, firstPos, first_edge)] + [
                PartialSplit(i+1, first_edge + i*splitSize, first_edge + (i+1) * splitSize)
                    for i in  range(ns)
                 ] + [PartialSplit(ns+1, last_edge, lastPos)]
        self.repr_name = '<%s %s (%d-%d)>' % (self.__class__.__name__, path, firstPos, lastPos)


class GZipFileRDD(TextFileRDD):
    "the gziped file must be seekable, compressed by pigz -i"
    BLOCK_SIZE = 64 << 10
    DEFAULT_SPLIT_SIZE = 32 << 20

    def __init__(self, ctx, path, splitSize=None):
        TextFileRDD.__init__(self, ctx, path, None, splitSize)

    def find_block(self, f, pos):
        f.seek(pos)
        block = f.read(32*1024)
        if len(block) < 4:
            f.seek(0, 2)
            return f.tell() # EOF
        ENDING = b'\x00\x00\xff\xff'
        while True:
            p = block.find(ENDING)
            while p < 0:
                pos += max(len(block) - 3, 0)
                block = block[-3:] + f.read(32<<10)
                if len(block) < 4:
                    return pos + 3 # EOF
                p = block.find(ENDING)
            pos += p + 4
            block = block[p+4:]
            if len(block) < 4096:
                block += f.read(4096)
                if not block:
                    return pos # EOF
            try:
                dz = zlib.decompressobj(-zlib.MAX_WBITS)
                if dz.decompress(block) and len(dz.unused_data) <= 8:
                    return pos # FOUND
            except Exception as e:
                pass

    def compute(self, split):
        with closing(self.open_file()) as f:
            last_line = b''
            if split.index == 0:
                zf = gzip.GzipFile(mode='r', fileobj=f)
                if hasattr(zf, '_buffer'):
                    zf._buffer.raw._read_gzip_header()
                else:
                    zf._read_gzip_header()

                zf.close()

                start = f.tell()
            else:
                start = self.find_block(f, split.index * self.splitSize)
                if start >= split.index * self.splitSize + self.splitSize:
                    return
                for i in range(1, 100):
                    if start - i * self.BLOCK_SIZE <= 4:
                        break
                    last_block = self.find_block(f, start - i * self.BLOCK_SIZE)
                    if last_block < start:
                        f.seek(last_block)
                        d = f.read(start - last_block)
                        dz = zlib.decompressobj(-zlib.MAX_WBITS)
                        _, sep, last_line = dz.decompress(d).rpartition(b'\n')
                        if sep:
                            break

            end = self.find_block(f, split.index * self.splitSize + self.splitSize)
            # TODO: speed up
            f.seek(start)
            f.length = end
            dz = zlib.decompressobj(-zlib.MAX_WBITS)
            skip_first = False
            while start < end:
                d = f.read(min(64<<10, end-start))
                start += len(d)
                if not d: break

                try:
                    io = BytesIO(dz.decompress(d))
                except Exception as e:
                    if self.err < 1e-6:
                        logger.error("failed to decompress file: %s", self.path)
                        raise
                    old = start
                    start = self.find_block(f, start)
                    f.seek(start)
                    logger.error("drop corrupted block (%d bytes) in %s",
                            start - old + len(d), self.path)
                    skip_first = True
                    continue

                if len(dz.unused_data) > 8 :
                    f.seek(-len(dz.unused_data)+8, 1)
                    zf = gzip.GzipFile(mode='r', fileobj=f)
                    if hasattr(zf, '_buffer'):
                        zf._buffer.raw._read_gzip_header()
                    else:
                        zf._read_gzip_header()
                    zf.close()
                    dz = zlib.decompressobj(-zlib.MAX_WBITS)
                    start -= f.tell()

                last_line += io.readline()
                if skip_first:
                    skip_first = False
                elif last_line.endswith(b'\n'):
                    line = last_line[:-1]
                    if not six.PY2:
                        line = line.decode('utf-8')
                    yield line
                last_line = b''

                ll = list(io)
                if not ll: continue

                last_line = ll.pop()
                for line in ll:
                    line = line[:-1]
                    if not six.PY2:
                        line = line.decode('utf-8')
                    yield line
                if last_line.endswith(b'\n'):
                    line = last_line[:-1]
                    if not six.PY2:
                        line = line.decode('utf-8')
                    yield line
                    last_line = b''


class TableFileRDD(TextFileRDD):

    DEFAULT_SPLIT_SIZE = 32 << 20

    def __init__(self, ctx, path, splitSize=None):
        TextFileRDD.__init__(self, ctx, path, None, splitSize)

    def find_magic(self, f, pos, magic):
        f.seek(pos)
        block = f.read(32*1024)
        if len(block) < len(magic):
            return -1
        p = block.find(magic)
        while p < 0:
            pos += len(block) - len(magic) + 1
            block = block[1 - len(magic):] + f.read(32<<10)
            if len(block) == len(magic) - 1:
                return -1
            p = block.find(magic)
        return pos + p

    def compute(self, split):
        import msgpack
        with closing(self.open_file()) as f:
            magic = f.read(8)
            start = split.index * self.splitSize
            end = (split.index + 1) * self.splitSize
            start = self.find_magic(f, start, magic)
            if start < 0:
                return
            f.seek(start)
            hdr_size = 12
            while start < end:
                m = f.read(len(magic))
                if m != magic:
                    break
                compressed, count, size = struct.unpack("III", f.read(hdr_size))
                d = f.read(size)
                assert len(d) == size, 'unexpected end'
                if compressed:
                    d = zlib.decompress(d)
                for r in msgpack.Unpacker(BytesIO(d)):
                    yield r
                start += len(magic) + hdr_size + size


class BZip2FileRDD(TextFileRDD):
    "the bzip2ed file must be seekable, compressed by pbzip2"

    DEFAULT_SPLIT_SIZE = 32*1024*1024
    BLOCK_SIZE = 9000

    def __init__(self, ctx, path, numSplits=None, splitSize=None):
        TextFileRDD.__init__(self, ctx, path, numSplits, splitSize)

    def compute(self, split):
        with closing(self.open_file()) as f:
            magic = f.read(10)
            f.seek(split.index * self.splitSize)
            d = f.read(self.splitSize)
            fp = d.find(magic)
            if fp > 0:
                d = d[fp:] # drop end of last block

            # real all the block
            nd = f.read(self.BLOCK_SIZE)
            np = nd.find(magic)
            while nd and np < 0:
                t = f.read(len(nd))
                if not t: break
                nd += t
                np = nd.find(magic)
            d += nd[:np] if np >= 0 else nd

            last_line = b''
            if split.index > 0:
                cur = split.index * self.splitSize
                skip = fp if fp >= 0 else d.find(magic)
                if skip >= 0:
                    cur += skip
                else:
                    cur += len(d)

                for i in range(1, 100):
                    pos = cur - i * self.BLOCK_SIZE
                    if pos < 0:
                        break

                    f.seek(pos)
                    nd = f.read(cur - pos)
                    np = nd.find(magic)
                    if np >= 0:
                        nd = nd[np:]
                        last_line = bz2.decompress(nd).split(b'\n')[-1]
                        break


        while d:
            np = d.find(magic, len(magic))
            if np <=0:
                data = d
            else:
                data = d[:np]
            try:
                io = BytesIO(bz2.decompress(data))
            except IOError as e:
                #bad position, skip it
                pass
            else:
                last_line += io.readline()
                if last_line.endswith(b'\n'):
                    line = last_line[:-1]
                    if not six.PY2:
                        line = line.decode('utf-8')
                    yield line
                    last_line = b''

                for line in io:
                    if line.endswith(b'\n'): # drop last line
                        line = line[:-1]
                        if not six.PY2:
                            line = line.decode('utf-8')
                        yield line
                    else:
                        last_line = line

            if np <= 0:
                break
            d = d[np:]


class BinaryFileRDD(TextFileRDD):
    def __init__(self, ctx, path, fmt=None, length=None, numSplits=None, splitSize=None):
        self.fmt = fmt
        if fmt:
            length = struct.calcsize(fmt)
        self.length = length
        assert length, "fmt or length must been provided"
        if splitSize is None:
            splitSize = self.DEFAULT_SPLIT_SIZE

        splitSize = max(splitSize // length, 1) * length
        TextFileRDD.__init__(self, ctx, path, numSplits, splitSize)
        self.repr_name = '<BinaryFileRDD(%s) %s>' % (fmt, path)

    def compute(self, split):
        start = split.index * self.splitSize
        end = min(start + self.splitSize, self.size)

        with closing(self.open_file()) as f:
            f.seek(start)
            rlen = self.length
            fmt = self.fmt
            for i in range((end - start) // rlen):
                d = f.read(rlen)
                if len(d) < rlen: break
                if fmt:
                    d = struct.unpack(fmt, d)
                yield d


class OutputTextFileRDD(DerivedRDD):
    def __init__(self, rdd, path, ext='', overwrite=False, compress=False):
        if os.path.exists(path):
            if not os.path.isdir(path):
                raise Exception("output must be dir")
            if overwrite:
                for n in os.listdir(path):
                    p = os.path.join(path, n)
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
        else:
            os.makedirs(path)

        DerivedRDD.__init__(self, rdd)
        self.path = os.path.abspath(path)
        if ext and not ext.startswith('.'):
            ext = '.' + ext
        if compress and not ext.endswith('gz'):
            ext += '.gz'
        self.ext = ext
        self.overwrite = overwrite
        self.compress = compress
        self.repr_name = '<%s %s %s>' % (self.__class__.__name__, path, rdd)

    def compute(self, split):
        path = os.path.join(self.path,
            "%04d%s" % (split.index, self.ext))
        if os.path.exists(path) and not self.overwrite:
            return

        with atomic_file(path, mode='wb', bufsize=4096 * 1024 * 16) as f:
            if self.compress:
                have_data = self.write_compress_data(f, self.prev.iterator(split))
            else:
                have_data = self.writedata(f, self.prev.iterator(split))

            if not have_data:
                raise AbortFileReplacement

        if os.path.exists(path):
            yield path

    def writedata(self, f, lines):
        if not six.PY2:
            f = TextIOWrapper(f)

        it = iter(lines)
        try:
            line = next(it)
        except StopIteration:
            return False
        f.write(line)
        if line.endswith('\n'):
            f.write(''.join(it))
        else:
            f.write('\n')
            s = '\n'.join(it)
            if s:
                f.write(s)
                f.write('\n')
        if not six.PY2:
            f.close()
        return True

    def write_compress_data(self, f, lines):
        empty = True
        with gzip.GzipFile(filename='', mode='w', fileobj=f) as f:
            if not six.PY2:
                f = TextIOWrapper(f)
            size = 0
            for line in lines:
                f.write(line)
                if not line.endswith('\n'):
                    f.write('\n')
                size += len(line) + 1
                if size >= 256 << 10:
                    f.flush()
                    f.compress = zlib.compressobj(9, zlib.DEFLATED,
                        -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0)
                    size = 0
                empty = False
            if not empty:
                f.flush()
            if not six.PY2:
                f.close()

        return not empty

class OutputTfrecordstFileRDD(OutputTextFileRDD):
    def __init__(self, rdd, path, ext, overwrite=True, compress=False):
        OutputTextFileRDD.__init__(self, rdd=rdd, path=path, ext='.tfrecords', overwrite=overwrite, compress=compress)

    def writedata(self, f, strings):
        empty = True
        for string in strings:
            string_bytes = str(string).encode()
            encoded_length = struct.pack('<Q', len(string_bytes))
            f.write(encoded_length + struct.pack('<I', masked_crc32c(encoded_length)) +
                       string_bytes + struct.pack('<I', masked_crc32c(string_bytes)))
            empty = False
        return not empty

    def write_compress_data(self, f, strings):
        empty = True
        with gzip.GzipFile(filename='', mode='wb', fileobj=f) as f:
            size = 0
            for string in strings:
                string_bytes = str(string).encode()
                encoded_length = struct.pack('<Q', len(string_bytes))
                f.write(encoded_length + struct.pack('<I', masked_crc32c(encoded_length)) +
                        string_bytes + struct.pack('<I', masked_crc32c(string_bytes)))
                size += len(str(string)) + 16
                if size >= 256 << 10:
                    f.flush()
                    f.compress = zlib.compressobj(9, zlib.DEFLATED,
                                                  -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0)
                    size = 0
                empty = False
            if not empty:
                f.flush()
            if not six.PY2:
                f.close()
        return not empty

class MultiOutputTextFileRDD(OutputTextFileRDD):
    MAX_OPEN_FILES = 512
    BLOCK_SIZE = 256 << 10

    def get_tpath(self, key):
        tpath = self.paths.get(key)
        if not tpath:
            dpath = os.path.join(self.path, str(key))
            mkdir_p(dpath)
            tpath = os.path.join(dpath,
                ".%04d%s.%s.%d.tmp" % (self.split.index, self.ext,
                socket.gethostname(), os.getpid()))
            self.paths[key] = tpath
        return tpath

    def get_file(self, key):
        f = self.files.get(key)
        fileobj = getattr(f, 'fileobj', None)
        if hasattr(f, 'buffer'):
            fileobj = f.buffer.fileobj

        if f is None or self.compress and fileobj is None:
            tpath = self.get_tpath(key)
            try:
                nf = open(tpath,'ab+', 4096 * 1024)
            except IOError:
                time.sleep(1) # there are dir cache in mfs for 1 sec
                nf = open(tpath,'ab+', 4096 * 1024)
            if self.compress:
                if f:
                    f.fileobj = nf
                else:
                    f = gzip.GzipFile(filename='', mode='a+', fileobj=nf)

                f.myfileobj = nf # force f.myfileobj.close() in f.close()
            else:
                f = nf

            if not six.PY2:
                f = TextIOWrapper(f)
            self.files[key] = f

        return f

    def flush_file(self, key, f):
        f.flush()
        if self.compress:
            f.compress = zlib.compressobj(9, zlib.DEFLATED,
                -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0)

        if len(self.files) > self.MAX_OPEN_FILES:
            if self.compress:
                open_files = sum(1 for f in self.files.values() if f.fileobj is not None)
                if open_files > self.MAX_OPEN_FILES:
                    f.fileobj.close()
                    f.fileobj = None
            else:
                f.close()
                self.files.pop(key)

    def compute(self, split):
        self.split = split
        self.paths = {}
        self.files = {}

        buffers = {}
        try:
            for k, v in self.prev.iterator(split):
                b = buffers.get(k)
                if b is None:
                    b = StringIO()
                    buffers[k] = b

                b.write(v)
                if not v.endswith('\n'):
                    b.write('\n')

                if b.tell() > self.BLOCK_SIZE:
                    f = self.get_file(k)
                    f.write(b.getvalue())
                    self.flush_file(k, f)
                    del buffers[k]

            for k, b in buffers.items():
                f = self.get_file(k)
                f.write(b.getvalue())
                f.close()
                del self.files[k]

            for k, f in self.files.items():
                if self.compress:
                    f = self.get_file(k) # make sure fileobj is open
                f.close()

            for k, tpath in self.paths.items():
                path = os.path.join(self.path, str(k), "%04d%s" % (split.index, self.ext))
                if not os.path.exists(path):
                    os.rename(tpath, path)
                    yield path
        finally:
            for k, tpath in self.paths.items():
                try:
                    os.remove(tpath)
                except:
                    pass


class OutputCSVFileRDD(OutputTextFileRDD):
    def __init__(self, rdd, path, dialect, overwrite, compress):
        OutputTextFileRDD.__init__(self, rdd, path, '.csv', overwrite, compress)
        self.dialect = dialect

    def writedata(self, f, rows):
        if not six.PY2:
            f = TextIOWrapper(f)

        writer = csv.writer(f, self.dialect)
        empty = True
        for row in rows:
            if not isinstance(row, (tuple, list)):
                row = (row,)
            writer.writerow(row)
            empty = False
        if not six.PY2:
            f.close()
        return not empty

    def write_compress_data(self, f, rows):
        empty = True
        with gzip.GzipFile(filename='', mode='w', fileobj=f) as f:
            if not six.PY2:
                f = TextIOWrapper(f)
            writer = csv.writer(f, self.dialect)
            last_flush = 0
            for row in rows:
                if not isinstance(row, (tuple, list)):
                    row = (row,)
                writer.writerow(row)
                empty = False
                if f.tell() - last_flush >= 256 << 10:
                    f.flush()
                    f.compress = zlib.compressobj(9, zlib.DEFLATED,
                        -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0)
                    last_flush = f.tell()
            if not empty:
                f.flush()
            if not six.PY2:
                f.close()
        return not empty

class OutputBinaryFileRDD(OutputTextFileRDD):
    def __init__(self, rdd, path, fmt, overwrite):
        OutputTextFileRDD.__init__(self, rdd, path, '.bin', overwrite)
        self.fmt = fmt

    def writedata(self, f, rows):
        empty = True
        for row in rows:
            if isinstance(row, (tuple, list)):
                f.write(struct.pack(self.fmt, *row))
            else:
                f.write(struct.pack(self.fmt, row))
            empty = False
        return not empty

class OutputTableFileRDD(OutputTextFileRDD):
    MAGIC = b'\x00\xDE\x00\xAD\xFF\xBE\xFF\xEF'
    BLOCK_SIZE = 256 << 10 # 256K

    def __init__(self, rdd, path, overwrite=True, compress=True):
        OutputTextFileRDD.__init__(self, rdd, path, ext='.tab', overwrite=overwrite, compress=False)
        self.compress = compress

    def writedata(self, f, rows):
        import msgpack
        def flush(buf):
            d = buf.getvalue()
            if self.compress:
                d = zlib.compress(d, 1)
            f.write(self.MAGIC)
            f.write(struct.pack("III", self.compress, count, len(d)))
            f.write(d)

        count, buf = 0, BytesIO()
        for row in rows:
            msgpack.pack(row, buf)
            count += 1
            if buf.tell() > self.BLOCK_SIZE:
                flush(buf)
                count, buf = 0, BytesIO()

        if count > 0:
            flush(buf)

        return f.tell() > 0

    write_compress_data = writedata


class BeansdbFileRDD(TextFileRDD):

    def __init__(self, ctx, path, filter=None, fullscan=False, raw=False):
        key_filter = filter
        if key_filter is None:
            fullscan = True
        TextFileRDD.__init__(
            self, ctx, path, numSplits=None if fullscan else 1)
        self.reader = BeansdbReader(path, key_filter, fullscan, raw)

    def compute(self, split):
        return self.reader.read(split.begin, split.end)


class OutputBeansdbRDD(DerivedRDD):

    def __init__(self, rdd, path, depth, overwrite, compress=False,
                 raw=False, value_with_meta=False):
        DerivedRDD.__init__(self, rdd)
        self.writer = BeansdbWriter(path, depth, overwrite, compress,
                                    raw, value_with_meta)
        self.repr_name = '<%s %s %s>' % (
            self.__class__.__name__, path, self.prev)

    def compute(self, split):
        return self.writer.write_bucket(self.prev.iterator(split), split.index)
