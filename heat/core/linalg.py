import collections
import itertools
import torch

from .communication import MPI
from . import dndarray
from . import factories
from . import manipulations
from . import tiling
from . import types

__all__ = ["dot", "matmul", "qr", "transpose", "tril", "triu"]


def dot(a, b, out=None):
    """
    Dot product of two arrays. Specifically,

    1. If both a and b are 1-D arrays, it is inner product of vectors.
    2. If both a and b are 2-D arrays, it is matrix multiplication. Using matmul or`a @ b` is recommended.
    3. If either a or b is 0-D (scalar), it is equivalent to multiply. Using `ht.multiply(a, b)` or `a * b` is recommended.

    Parameters
    ----------
    a : ht.DNDarray
    b : ht.DNDarray
    out : ht.DNDarray or None, optional
            A location in which to store the results. If provided, it must have a broadcastable
            shape. If not provided or set to None, a fresh tensor is allocated.

    Returns
    -------
    ht.DNDarray or single value (float or int)
        Returns the dot product of a and b. If a and b are both scalars or both 1-D arrays then a
        scalar is returned; otherwise an array is returned. If out is given, then it is returned.
    """
    if (
        isinstance(a, (float, int))
        or isinstance(b, (float, int))
        or a.numdims == 0
        or b.numdims == 0
    ):
        # 3. If either a or b is 0-D (scalar), it is equivalent to multiply and using
        # numpy.multiply(a, b) or a * b is preferred.
        if out is not None:
            out = a * b
            return out
        return a * b
    elif a.numdims == 1 and b.numdims == 1:
        # 1. If both a and b are 1-D arrays, it is inner product of vectors.
        if a.split is None and b.split is None:
            sl = slice(None)
        else:  # at least one of them is split
            sl = a.comm.chunk(a.shape, a.split if a.split is not None else b.split)[2]
        ret = torch.dot(a[sl]._DNDarray__array, b[sl]._DNDarray__array)
        if a.is_distributed() or b.is_distributed():
            a.comm.Allreduce(MPI.IN_PLACE, ret, MPI.SUM)

        if out is not None:
            out = ret.item()
            return out
        return ret.item()
    elif a.numdims == 2 and b.numdims == 2:
        # 2. If both a and b are 2-D arrays, it is matrix multiplication,
        # but using matmul or a @ b is preferred.
        ret = matmul(a, b)
        if out is not None:
            if out is not None:
                out._DNDarray__array = ret._DNDarray__array
                out._DNDarray__dtype = ret.dtype
                out._DNDarray__split = ret.split
                out._DNDarray__device = ret.device
                out._DNDarray__comm = ret.comm
            return out
        return ret
    else:
        raise NotImplementedError("ht.dot not implemented for N-D dot M-D arrays")


def matmul(a, b, allow_resplit=False):
    """
    Matrix multiplication of two DNDarrays

    for comment context -> a @ b = c or A @ B = c

    Parameters
    ----------
    a : ht.DNDarray
        2 dimensional: L x P
    b : ht.DNDarray
        2 dimensional: P x Q
    allow_resplit : bool, optional
        Flag for if to resplit the DNDarray 'a' in the case that both 'a' and 'b' are not split.
        Default: if both are not split then both will remain not split.
        True: if both are not split then 'a' will be split in-place along axis 0, i.e. the split
            axis of 'a' will become 0 and the DNDarray will be distributed in the standard fashion.
            The default case should be the most efficient case for large matrices.

    Returns
    -------
    ht.DNDarray
        returns a tensor with the result of a @ b. The split dimension of the returned array is
        typically the split dimension of a. However, if a.split = None then the the c.split will be
        set as the split dimension of b. If both are None then c.split is also None.

    Notes
    -----
    - If a is a split vector then the returned vector will be of shape (1xQ) and will be split in
        the 1st dimension
    - If b is a vector and either a or b is split, then the returned vector will be of shape (Lx1)
        and will be split in the 0th dimension

    References
    ----------
    [1] R. Gu, et al., "Improving Execution Concurrency of Large-scale Matrix Multiplication on
        Distributed Data-parallel Platforms," IEEE Transactions on Parallel and Distributed Systems,
         vol 28, no. 9. 2017.
    [2] S. Ryu and D. Kim, "Parallel Huge Matrix Multiplication on a Cluster with GPGPU
        Accelerators," 2018 IEEE International Parallel and Distributed Processing Symposium
        Workshops (IPDPSW), Vancouver, BC, 2018, pp. 877-882.

    Example
    -------
    >>> a = ht.ones((n, m), split=1)
    >>> a[0] = ht.arange(1, m + 1)
    >>> a[:, -1] = ht.arange(1, n + 1)
    (0/1) tensor([[1., 2.],
                  [1., 1.],
                  [1., 1.],
                  [1., 1.],
                  [1., 1.]])
    (1/1) tensor([[3., 1.],
                  [1., 2.],
                  [1., 3.],
                  [1., 4.],
                  [1., 5.]])
    >>> b = ht.ones((j, k), split=0)
    >>> b[0] = ht.arange(1, k + 1)
    >>> b[:, 0] = ht.arange(1, j + 1)
    (0/1) tensor([[1., 2., 3., 4., 5., 6., 7.],
                  [2., 1., 1., 1., 1., 1., 1.]])
    (1/1) tensor([[3., 1., 1., 1., 1., 1., 1.],
                  [4., 1., 1., 1., 1., 1., 1.]])
    >>> linalg.matmul(a, b)
    (0/1) tensor([[18.,  8.,  9., 10.],
                  [14.,  6.,  7.,  8.],
                  [18.,  7.,  8.,  9.],
                  [22.,  8.,  9., 10.],
                  [26.,  9., 10., 11.]])
    (1/1) tensor([[11., 12., 13.],
                  [ 9., 10., 11.],
                  [10., 11., 12.],
                  [11., 12., 13.],
                  [12., 13., 14.]])
    """
    if a.gshape[-1] != b.gshape[0]:
        raise ValueError(
            "If the last dimension of a ({}) is not the same size "
            "as the second-to-last dimension of b. ({})".format(a.gshape[-1], b.gshape[-2])
        )

    # determine if a larger type is needed for c
    c_type = types.promote_types(a.dtype, b.dtype)
    if a.dtype != c_type:
        a = c_type(a, device=a.device)
    if b.dtype != c_type:
        b = c_type(b, device=b.device)

    if a.split is None and b.split is None:  # matmul from torch
        if len(a.gshape) < 2 or len(b.gshape) < 2 or not allow_resplit:
            # if either of A or B is a vector
            # or if the inputs should not be split
            return factories.array(
                torch.matmul(a._DNDarray__array, b._DNDarray__array), device=a.device
            )
        else:
            a.resplit_(0)
            slice_0 = a.comm.chunk(a.shape, a.split)[2][0]
            hold = a._DNDarray__array @ b._DNDarray__array

            c = factories.zeros((a.gshape[-2], b.gshape[1]), dtype=c_type, device=a.device)
            c._DNDarray__array[slice_0.start : slice_0.stop, :] += hold
            c.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            return c
    else:
        # if they are vectors they need to be expanded to be the proper dimensions
        vector_flag = False  # flag to run squeeze at the end of the function
        both_vec = 0
        if len(a.gshape) < 2:
            a = manipulations.expand_dims(a, axis=0)
            vector_flag = True
            both_vec += 1
        if len(b.gshape) < 2:
            b = manipulations.expand_dims(b, axis=1)
            vector_flag = True
            both_vec += 1
        both_vec = True if both_vec == 2 else False

        split_0_flag = False
        split_1_flag = False
        split_01_flag = False
        split_10_flag = False

        if (
            (a.split == 0 and b.split is None) or (a.split is None and b.split == 1)
        ) and not vector_flag:
            split = a.split if a.split is not None else b.split
            split = split if not vector_flag else 0
            c = factories.zeros(
                (a.gshape[-2], b.gshape[1]), split=split, dtype=c_type, device=a.device
            )
            c._DNDarray__array += a._DNDarray__array @ b._DNDarray__array

            return c if not vector_flag else c.squeeze()

        elif a.split == 1 and b.split is None:
            c = torch.zeros(
                (a.gshape[-2], b.gshape[1]), dtype=c_type.torch_type(), device=a.device.torch_device
            )

            a_idx = a.comm.chunk(a.shape, a.split)[2]
            c += (
                a._DNDarray__array
                @ b._DNDarray__array[a_idx[1].start : a_idx[1].start + a.lshape[-1], :]
            )
            a.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            c = c if not vector_flag else c.squeeze()
            c = factories.array(c, split=a.split if b.gshape[1] > 1 else 0, device=a.device)
            return c

        elif a.split is None and b.split == 0:
            c = torch.zeros(
                (a.gshape[-2], b.gshape[1]), dtype=c_type.torch_type(), device=a.device.torch_device
            )
            b_idx = b.comm.chunk(b.shape, b.split)[2]
            c += (
                a._DNDarray__array[:, b_idx[0].start : b_idx[0].start + b.lshape[0]]
                @ b._DNDarray__array
            )
            b.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            c = c if not vector_flag else c.squeeze()
            c = factories.array(c, split=b.split if a.gshape[-2] > 1 else 0, device=a.device)
            return c

        elif (
            a.split == 0 and b.split is None
        ):  # this case and the one below will only be reaching if one of them is a vector
            c = torch.zeros(
                (a.gshape[-2], b.lshape[1]), dtype=c_type.torch_type(), device=a.device.torch_device
            )
            a_idx = a.comm.chunk(a.shape, a.split)[2]
            c[a_idx[0]] += a._DNDarray__array @ b._DNDarray__array
            a.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            c = c if not vector_flag else c.squeeze()
            split = a.split if b.gshape[1] > 1 else 0
            split = split if not vector_flag else 0
            c = factories.array(c, split=split, device=a.device)
            return c

        elif a.split is None and b.split == 1:
            c = torch.zeros(
                (a.gshape[-2], b.lshape[1]), dtype=c_type.torch_type(), device=a.device.torch_device
            )
            c += a._DNDarray__array @ b._DNDarray__array
            c = c if not vector_flag else c.squeeze()
            split = b.split if a.gshape[1] > 1 else 0
            split = split if not vector_flag else 0
            c = factories.array(c, is_split=split, device=a.device)
            return c

        elif a.split == 0 and b.split == 0:
            split_0_flag = True
        elif a.split == 1 and b.split == 1:
            split_1_flag = True
        elif a.split == 0 and b.split == 1:
            split_01_flag = True
        elif a.split == 1 and b.split == 0:
            split_10_flag = True
        else:
            raise NotImplementedError("splits > 1 not implemented")

        # block sizes dont need to be the same. thy just need the same inner dimmension (kB)
        kB = 0
        rem_a, rem_b = [0] * 2
        if (
            a.split == len(a.gshape) - 1 and b.split == len(a.gshape) - 2
        ):  # if the split direction is the last dim in a and the first dim in b
            # the max inner dim (kB) is the min value from the result of the integer division of
            # the last dim of a/world size and the first dim of b/world size
            kB = min([a.gshape[-1] // a.comm.size, b.gshape[0] // b.comm.size])
        elif a.split == len(a.gshape) - 2 and b.split == len(a.gshape) - 1:
            kB = a.gshape[-1]
        elif a.split == len(a.gshape) - 1:
            kB = a.gshape[-1] // a.comm.size
        elif b.split == len(a.gshape) - 2:
            kB = b.gshape[0] // b.comm.size
            kB = kB if kB < a.gshape[-1] else a.gshape[-1]

        if a.lshape[-1] % kB != 0 or (kB == 1 and a.lshape[-1] != 1):
            rem_a = 1
        if b.lshape[0] % kB != 0 or (kB == 1 and b.lshape[-2] != 1):
            rem_b = 1

        # get the lshape map to determine what needs to be sent where as well as M and N
        # lshape map dims -> {node, a=0, b=1, lshape}
        lshape_map = torch.zeros(
            (a.comm.size, 2, len(a.gshape)), dtype=int, device=a.device.torch_device
        )
        lshape_map[a.comm.rank, 0, :] = torch.tensor(a.lshape, device=a.device.torch_device)
        lshape_map[b.comm.rank, 1, :] = torch.tensor(b.lshape, device=a.device.torch_device)
        a.comm.Allreduce(MPI.IN_PLACE, lshape_map, MPI.SUM)

        # find mB (first blocking dim for a) and nB (2nd blocking dim for b)
        mB = lshape_map[:, 0, -2].min().item()
        nB = lshape_map[:, 1, -1].min().item()

        # check for remaining dims in the outside dimensions
        rem_a_out, rem_b_out = 0, 0
        if a.lshape[-2] % mB != 0 or (kB == 1 and a.lshape[-2] != 1):
            rem_a_out = 1
        if b.lshape[-1] % nB != 0 or (kB == 1 and b.lshape[-1] != 1):
            rem_b_out = 1

        # get the flags from all processes
        # rem_map dims guide
        #   -> {process number, a/b (0/1), True/False (1/0) if there is a remainder in this dim
        rem_map = torch.zeros((a.comm.size, 2, 2), device=a._DNDarray__array.device)
        rem_map[a.comm.rank, 0, :] = torch.tensor(
            (rem_a_out, rem_a), device=a._DNDarray__array.device
        )
        rem_map[a.comm.rank, 1, :] = torch.tensor(
            (rem_b, rem_b_out), device=a._DNDarray__array.device
        )
        rem_map_comm = a.comm.Iallreduce(MPI.IN_PLACE, rem_map, MPI.SUM)

        # index_map dims guide -> {process number, a=0/b=1, relevent 1st index, 2nd index}
        index_map = torch.zeros((a.comm.size, 2, 2, 2), dtype=int, device=b._DNDarray__array.device)
        a_idx = a.comm.chunk(a.shape, a.split)[2]
        index_map[a.comm.rank, 0, 0] = torch.tensor(
            (a_idx[0].start, a_idx[0].stop), device=b._DNDarray__array.device
        )
        index_map[a.comm.rank, 0, 1] = torch.tensor(
            (a_idx[1].start, a_idx[1].stop), device=b._DNDarray__array.device
        )
        b_idx = b.comm.chunk(b.shape, b.split)[2]
        index_map[b.comm.rank, 1, 0] = torch.tensor(
            (b_idx[0].start, b_idx[0].stop), device=b._DNDarray__array.device
        )
        index_map[b.comm.rank, 1, 1] = torch.tensor(
            (b_idx[1].start, b_idx[1].stop), device=b._DNDarray__array.device
        )
        index_map_comm = a.comm.Iallreduce(MPI.IN_PLACE, index_map, MPI.SUM)

        # for the communication scheme, the output array needs to be created
        c_shape = (a.gshape[-2], b.gshape[1])
        c = factories.zeros(c_shape, split=a.split, dtype=c_type, device=a.device)

        # get the index map for c
        c_index_map = factories.zeros((c.comm.size, 2, 2), device=a.device)
        c_idx = c.comm.chunk(c.shape, c.split)[2]
        c_index_map[c.comm.rank, 0, :] = (c_idx[0].start, c_idx[0].stop)
        c_index_map[c.comm.rank, 1, :] = (c_idx[1].start, c_idx[1].stop)
        c_wait = c.comm.Iallreduce(MPI.IN_PLACE, c_index_map, MPI.SUM)

        if a.split == 0:
            a_block_map = torch.zeros(
                (a.comm.size, a.shape[-2] // mB // a.comm.size, a.shape[-1] // kB, 2),
                dtype=torch.int,
                device=a.device.torch_device,
            )
        elif a.split == 1:
            a_block_map = torch.zeros(
                (a.comm.size, a.shape[-2] // mB, a.shape[-1] // kB // a.comm.size, 2),
                dtype=torch.int,
                device=a.device.torch_device,
            )
        # units-> [process, dim0 block number, dim1 block number, start coord] **indices are local

        # below is to handle the edge case where there is only one element in one dimension of a
        a_d0_1s_flag, a_d1_1s_flag = False, False
        if any(lshape_map[:, 0, :][:, 0] == 1):
            a_d0_1s_flag = True
        if any(lshape_map[:, 0, :][:, 1] == 1):
            a_d1_1s_flag = True

        index_map_comm.wait()
        for pr in range(a.comm.size):
            start0 = index_map[pr, 0, 0, 0].item()
            stop0 = index_map[pr, 0, 0, 1].item()
            start1 = index_map[pr, 0, 1, 0].item()
            stop1 = index_map[pr, 0, 1, 1].item()

            for dim0 in range(
                (stop0 - start0) // mB // a.comm.size if a_d0_1s_flag else (stop0 - start0) // mB
            ):
                # loop over the number of blocks in the 0th dimension
                for dim1 in range(
                    (stop1 - start1) // kB // a.comm.size
                    if a_d1_1s_flag
                    else (stop1 - start1) // kB
                ):
                    # loop over the number of blocks in the 1st dimension
                    a_block_map[pr, dim0, dim1] = torch.tensor(
                        (dim0 * mB, dim1 * kB), dtype=torch.int, device=a._DNDarray__array.device
                    )
        rem_map_comm.wait()
        if b.split == 0:
            # blocks are shifted in the 2nd dim of A for as many remainders there are between
            # the blocks in the first dim of B
            cnt = 0
            for r in rem_map[:, 1, 0]:
                if r.item():
                    cnt += 1
                    a_block_map[:, :, cnt:, 1] += 1

        if b.split == 0:
            b_block_map = torch.zeros(
                (b.comm.size, b.shape[-2] // kB // b.comm.size, b.shape[-1] // nB, 2),
                dtype=torch.int,
                device=b.device.torch_device,
            )
        if b.split == 1:
            b_block_map = torch.zeros(
                (b.comm.size, b.shape[-2] // kB, b.shape[-1] // nB // b.comm.size, 2),
                dtype=torch.int,
                device=b.device.torch_device,
            )
        # units-> [process, dim0 block number, dim1 block number, start coord] **indices are local

        # below is to handle the edge case where there is only one element in one dimension of b
        b_d0_1s_flag, b_d1_1s_flag = False, False
        if any(lshape_map[:, 1, :][:, 0] == 1):
            b_d0_1s_flag = True
        if any(lshape_map[:, 1, :][:, 1] == 1):
            b_d1_1s_flag = True

        for pr in range(b.comm.size):
            start0 = index_map[pr, 1, 0, 0].item()
            stop0 = index_map[pr, 1, 0, 1].item()
            start1 = index_map[pr, 1, 1, 0].item()
            stop1 = index_map[pr, 1, 1, 1].item()

            # loop over the number of blocks in the 0th dimension
            for dim0 in range(
                (stop0 - start0) // kB // b.comm.size if b_d0_1s_flag else (stop0 - start0) // kB
            ):
                # loop over the number of blocks in the 1st dimension
                for dim1 in range(
                    (stop1 - start1) // nB // b.comm.size
                    if b_d1_1s_flag
                    else (stop1 - start1) // nB
                ):
                    b_block_map[pr, dim0, dim1] = torch.tensor(
                        (dim0 * kB, dim1 * nB), dtype=torch.int, device=b._DNDarray__array.device
                    )

        if a.split == 1:
            cnt = 0
            # this loop will push the blocks in B to adjust for the remainders in A
            for r in rem_map[:, 0, 1]:
                if r.item():
                    cnt += 1
                    b_block_map[:, cnt:, :, 0] += 1

        # work loop: loop over all processes (also will incorporate the remainder calculations)
        c_wait.wait()

        if split_0_flag:
            # need to send b here and not a
            # locations of the remainders in b
            b_rem_locs0 = (rem_map[:, 1, 0] == 1).nonzero()
            a_rem_locs0 = (rem_map[:, 0, 0] == 1).nonzero()
            # remainders for a in the
            a_node_rem_s0 = a._DNDarray__array[:mB, kB : (kB + 1) * b_rem_locs0.numel() : kB + 1]
            b_rem = torch.empty(
                b_rem_locs0.numel(),
                b.lshape[-1],
                dtype=a.dtype.torch_type(),
                device=b.device.torch_device,
            )

            # this if/elif/else loop is for the handling of
            if a.comm.rank in a_rem_locs0:
                # if A is split in dim0 and the rank has a remainder in this direction
                r = a._DNDarray__array[-1]
                r_loc = index_map[a.comm.rank, 0, 0, 1] - index_map[a.comm.rank, 0, 0, 0] - 1
            else:
                r = None
                r_loc = None

            req = {}
            b_lp_data = {}
            for pr in range(b.comm.size):
                # ibcast data on node first
                if b.comm.rank == pr:
                    b_lp_data[pr] = b._DNDarray__array.clone()
                else:
                    b_lp_data[pr] = torch.zeros(
                        (lshape_map[pr, 1, 0].item(), lshape_map[pr, 1, 1].item()),
                        dtype=b.dtype.torch_type(),
                        device=b.device.torch_device,
                    )

                # sending a to all nodes for b to operate with
                req[pr] = b.comm.Ibcast(b_lp_data[pr], root=pr)

                # receive the data from the last loop and do the calculation with that
                if pr != 0:
                    req[pr - 1].wait()
                    # after receiving the last loop's bcast
                    __mm_c_block_setter(
                        b_proc=pr - 1,
                        a_proc=a.comm.rank,
                        a_data=a._DNDarray__array,
                        b_data=b_lp_data[pr - 1],
                        b_block_map=b_block_map,
                        a_block_map=a_block_map,
                        b_split=b.split,
                        a_split=a.split,
                        mB=mB,
                        kB=kB,
                        nB=nB,
                        c=c._DNDarray__array,
                    )

                    # check if there is a remainder on b in the previous node
                    # this loop gets the remainders of b since it is the one being passed
                    if pr - 1 in b_rem_locs0:
                        # takes care of the remainders in b as well as dim0 of a
                        b_rem[pr - 1] = b_lp_data[pr - 1][-1]

                    # this loop is to take care of the remainders in dim0 of A
                    if a_rem_locs0.nelement() != 0:
                        if r_loc is not None:
                            st = index_map[pr - 1, 1, 0, 0].item()
                            sp = index_map[pr - 1, 1, 0, 1].item()
                            c._DNDarray__array[r_loc.item(), :] += r[st:sp] @ b_lp_data[pr - 1]

                    del b_lp_data[pr - 1]

                # need to wait if its the last loop, also need to collect the remainders
                if pr == b.comm.size - 1:
                    req[pr].wait()
                    __mm_c_block_setter(
                        b_proc=pr,
                        a_proc=a.comm.rank,
                        a_data=a._DNDarray__array,
                        b_data=b_lp_data[pr],
                        b_block_map=b_block_map,
                        a_block_map=a_block_map,
                        b_split=b.split,
                        a_split=a.split,
                        mB=mB,
                        kB=kB,
                        nB=nB,
                        c=c._DNDarray__array,
                    )
                    # check if there is a remainder on b on the last node (there shouldnt be)
                    if pr in b_rem_locs0:
                        # this is to save the data from B required by the remainders from dim1 of A
                        b_rem[pr] = b_lp_data[pr][-1]

                    # this loop is to take care of the remainders in the 0th dimension of A
                    if a_rem_locs0.nelement() != 0:
                        if r_loc is not None:
                            st = index_map[pr, 1, 0, 0].item()
                            sp = index_map[pr, 1, 0, 1].item()

                            if split_01_flag:
                                st1 = index_map[pr, 1, 1, 0].item()
                                sp1 = index_map[pr, 1, 1, 1].item()
                                c._DNDarray__array[r_loc.item(), st1:sp1] += (
                                    r[st:sp] @ b_lp_data[pr]
                                )
                            else:
                                c._DNDarray__array[r_loc.item(), :] += r[st:sp] @ b_lp_data[pr]

                    # set the final blocks on the last loop, then adjust for the the remainders
                    # which were collected in b_rem
                    if b_rem_locs0.numel():
                        c._DNDarray__array[: a_node_rem_s0.shape[0]] += a_node_rem_s0 @ b_rem

                    del b_lp_data[pr]

            if vector_flag:
                c_loc = c._DNDarray__array.squeeze()
                if c_loc.nelement() == 1:
                    c = torch.tensor(c_loc, device=c._DNDarray__array.device)
                c = factories.array(c_loc, is_split=0, device=a.device)

            return c

        elif split_1_flag:
            # for this case, a is sent to b
            # locations of the remainders in b
            b_rem_locs1 = (rem_map[:, 1, 1] == 1).nonzero()
            a_rem_locs1 = (rem_map[:, 0, 1] == 1).nonzero()
            b_node_rem_s1 = b._DNDarray__array[
                kB : (kB + 1) * a_rem_locs1.numel() : kB + 1, :nB
            ]  # remainders for a in the
            a_rem = torch.empty(
                a.lshape[-2],
                a_rem_locs1.numel(),
                dtype=b.dtype.torch_type(),
                device=a.device.torch_device,
            )

            # this if/elif/else loop is for the handling of
            if b.comm.rank in b_rem_locs1:
                # if b is split in dim1 and the rank has a remainder in this direction
                r = b._DNDarray__array[:, -1]
                r_loc = index_map[a.comm.rank, 1, 1, 1] - index_map[a.comm.rank, 1, 1, 0] - 1
            else:
                r = None
                r_loc = None

            req = {}
            a_lp_data = {}
            for pr in range(a.comm.size):
                # ibcast data on node first
                if a.comm.rank == pr:
                    a_lp_data[pr] = a._DNDarray__array.clone()
                else:
                    a_lp_data[pr] = torch.zeros(
                        (lshape_map[pr, 0, 0].item(), lshape_map[pr, 0, 1].item()),
                        dtype=a.dtype.torch_type(),
                        device=a.device.torch_device,
                    )

                # sending a to all nodes for b to operate with
                req[pr] = a.comm.Ibcast(a_lp_data[pr], root=pr)

                # receive the data from the last loop and do the calculation with that
                if pr != 0:
                    # after receiving the last loop's bcast
                    req[pr - 1].wait()
                    __mm_c_block_setter(
                        a_proc=pr - 1,
                        b_proc=b.comm.rank,
                        a_data=a_lp_data[pr - 1],
                        b_data=b._DNDarray__array,
                        b_block_map=b_block_map,
                        a_block_map=a_block_map,
                        b_split=b.split,
                        a_split=a.split,
                        mB=mB,
                        kB=kB,
                        nB=nB,
                        c=c._DNDarray__array,
                    )

                    # check if there is a remainder on b in the previous node
                    # this loop is intended to get the rems of b since it is the one being passed
                    if pr - 1 in a_rem_locs1:
                        # takes care of the remainders in b as well as dim0 of a
                        a_rem[:, pr - 1] = a_lp_data[pr - 1][:, -1]

                    # this loop is to take care of the remainders in dim1 of B
                    if b_rem_locs1.nelement() != 0:
                        if r_loc is not None:
                            st = index_map[pr - 1, 0, 1, 0].item()
                            sp = index_map[pr - 1, 0, 1, 1].item()
                            c._DNDarray__array[:, r_loc.item()] += (
                                a_lp_data[pr - 1] @ r[st:sp, None]
                            ).flatten()

                    del a_lp_data[pr - 1]

                # need to wait if its the last loop, also need to collect the remainders
                if pr == b.comm.size - 1:
                    req[pr].wait()
                    __mm_c_block_setter(
                        a_proc=pr,
                        b_proc=a.comm.rank,
                        a_data=a_lp_data[pr],
                        b_data=b._DNDarray__array,
                        b_block_map=b_block_map,
                        a_block_map=a_block_map,
                        b_split=b.split,
                        a_split=a.split,
                        mB=mB,
                        kB=kB,
                        nB=nB,
                        c=c._DNDarray__array,
                    )
                    # check if there is a remainder on b on the last node (there shouldnt be)
                    if pr in a_rem_locs1:
                        # this is to save the data from B required by the remainders from dim1 of A
                        a_rem[:, pr] = a_lp_data[pr][:, -1]

                    # this loop is to take care of the remainders in the 0th dimension of A
                    if b_rem_locs1.nelement() != 0:
                        if r_loc is not None:
                            st = index_map[pr, 0, 1, 0].item()
                            sp = index_map[pr, 0, 1, 1].item()
                            c._DNDarray__array[:, r_loc.item()] += (
                                a_lp_data[pr] @ r[st:sp, None]
                            ).flatten()

                    # set the final blocks on the last loop,
                    # then adjust for the the remainders which were collected in b_rem
                    if a_rem_locs1.numel():
                        c._DNDarray__array[:, : b_node_rem_s1.shape[1]] += a_rem @ b_node_rem_s1

                    del a_lp_data[pr]
            c = (
                c
                if not vector_flag
                else factories.array(c._DNDarray__array.squeeze(), is_split=0, device=a.device)
            )
            return c

        elif split_01_flag:
            # for this case there are no remainders which need to be taken care of
            req = {}
            b_lp_data = {}
            for pr in range(a.comm.size):
                # ibcast data on node first
                if b.comm.rank == pr:
                    b_lp_data[pr] = b._DNDarray__array.clone()
                else:
                    b_lp_data[pr] = torch.empty(
                        (lshape_map[pr, 1, 0].item(), lshape_map[pr, 1, 1].item()),
                        dtype=b.dtype.torch_type(),
                        device=b.device.torch_device,
                    )

                # sending a to all nodes for b to operate with
                req[pr] = b.comm.Ibcast(b_lp_data[pr], root=pr)

                # receive the data from the last loop and do the calculation with that
                if pr != 0:
                    req[pr - 1].wait()
                    # after receiving the last loop's bcast
                    st0 = index_map[pr - 1, 0, 0, 0].item()
                    sp0 = index_map[pr - 1, 0, 0, 1].item() + 1
                    st1 = index_map[pr - 1, 1, 1, 0].item()
                    sp1 = index_map[pr - 1, 1, 1, 1].item()
                    c._DNDarray__array[: sp0 - st0, st1:sp1] += (
                        a._DNDarray__array @ b_lp_data[pr - 1]
                    )

                    del b_lp_data[pr - 1]

                if pr == b.comm.size - 1:
                    req[pr].wait()
                    st0 = index_map[pr, 0, 0, 0].item()
                    sp0 = index_map[pr, 0, 0, 1].item() + 1
                    st1 = index_map[pr, 1, 1, 0].item()
                    sp1 = index_map[pr, 1, 1, 1].item()
                    c._DNDarray__array[: sp0 - st0, st1:sp1] += a._DNDarray__array @ b_lp_data[pr]

                    del b_lp_data[pr]

            c = (
                c
                if not vector_flag
                else factories.array(c._DNDarray__array.squeeze(), is_split=0, device=a.device)
            )
            return c

        elif split_10_flag:
            # for this case, only a sum is needed at the end
            a_rem_locs1 = (rem_map[:, 0, 1] == 1).nonzero()
            # locations of the remainders in b
            b_rem_locs0 = (rem_map[:, 1, 0] == 1).nonzero()
            res = torch.zeros(
                (a.gshape[-2], b.gshape[1]), dtype=c_type.torch_type(), device=c.device.torch_device
            )
            for i in range(a.lshape[-1] // kB):
                res += (
                    a._DNDarray__array[:mB, i * kB : i * kB + kB]
                    @ b._DNDarray__array[i * kB : i * kB + kB, :nB]
                )
            if a.comm.rank in a_rem_locs1 and b.comm.rank in b_rem_locs0:
                # these Nones are used to change the dims
                res += a._DNDarray__array[:, -1, None] @ b._DNDarray__array[None, -1, :]

            a.comm.Allreduce(MPI.IN_PLACE, res, MPI.SUM)
            split = a.split if b.gshape[1] > 1 else 0
            split = split if not vector_flag else 0
            res = res if not vector_flag else res.squeeze()
            c = factories.array(res, split=split if not both_vec else None, device=a.device)
            return c


@torch.jit.script
def __mm_c_block_setter(
    b_proc, a_proc, a_data, b_data, b_block_map, a_block_map, b_split, a_split, mB, kB, nB, c
):
    # type: (int, int, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, int, int, int, int, int, torch.Tensor) -> None
    shp_b = b_block_map.shape
    offset_a = b_proc * shp_b[1] if b_proc != 0 else 0
    shp_a = a_block_map.shape
    offset_b = a_proc * shp_a[2] if a_proc != 0 else 0
    # offsets are the number of blocks in the multiplication direction on previous nodes
    for bl_1_a in (
        torch.arange(offset_a, offset_a + shp_b[1], dtype=torch.long, device=c.device)
        if b_split == 0
        else torch.arange(a_block_map[a_proc].shape[0], dtype=torch.long, device=c.device)
    ):
        # offset is the number of blocks on the previous node in the direction of multiplication
        for bl_0_a in torch.arange(
            a_block_map[a_proc].shape[0], dtype=torch.long, device=c.device
        ):  # dim0
            for bl_1_b in torch.arange(
                b_block_map[b_proc].shape[1], dtype=torch.long, device=c.device
            ):
                for bl_0_b in (
                    torch.arange(offset_b, offset_b + shp_a[1], dtype=torch.long, device=c.device)
                    if a_split == 1
                    else torch.arange(
                        b_block_map[b_proc].shape[0], dtype=torch.long, device=c.device
                    )
                ):
                    # this offset is the same as before but for b
                    a_start1 = int(a_block_map[a_proc, bl_0_a, bl_1_a, 1].item())
                    a_start0 = int(a_block_map[a_proc, bl_0_a, bl_1_a, 0].item())
                    a_block = a_data[a_start0 : a_start0 + mB, a_start1 : a_start1 + kB]

                    b_start0 = int(b_block_map[b_proc, bl_0_b, bl_1_b, 0].item())
                    b_start1 = int(b_block_map[b_proc, bl_0_b, bl_1_b, 1].item())
                    b_block = b_data[b_start0 : b_start0 + kB, b_start1 : b_start1 + nB]

                    c_start0 = a_start0
                    c_start1 = b_start1
                    c[c_start0 : c_start0 + mB, c_start1 : c_start1 + nB] += a_block @ b_block


def qr(a, tiles_per_proc=1, calc_q=True, overwrite_a=False):
    """
    Calculates the QR decomposition of a 2D DNDarray.
    Factor the matrix `a` as *qr*, where `q` is orthonormal and `r` is upper-triangular.

    Parameters
    ----------
    a : DNDarray
        DNDarray which will be decomposed
    tiles_per_proc : int, singlt element torch.Tensor
        optional, default: 1
        number of tiles per process to operate on
    calc_q : bool
        optional, default: True
        whether or not to calculate Q
        if True, function returns (Q, R)
        if False, function returns (None, R)
    overwrite_a : bool
        optional, default: False
        if True, function overwrites the DNDarray a, with R
        if False, a new array will be created for R

    Returns
    -------
    namedtuple of Q and R
        if calc_q == True, function returns QR(Q=Q, R=R)
        if calc_q == False, function returns QR(Q=None, R=R)

    Notes
    -----
    This function is built on top of PyTorch's QR function. torch.qr() using LAPACK on the backend.
    Basic information about QR factorization/decomposition can be found at
    https://en.wikipedia.org/wiki/QR_factorization

    The algorithms are based on the CAQR and TSQRalgorithms. For more information see references.

    References
    ----------
    [0]  W. Zheng, F. Song, L. Lin, and Z. Chen, “Scaling Up Parallel Computation of Tiled QR
            Factorizations by a Distributed Scheduling Runtime System and Analytical Modeling,”
            Parallel Processing Letters, vol. 28, no. 01, p. 1850004, 2018.
    [1] Bilel Hadri, Hatem Ltaief, Emmanuel Agullo, Jack Dongarra. Tile QR Factorization with
            Parallel Panel Processing for Multicore Architectures. 24th IEEE International Parallel
            and DistributedProcessing Symposium (IPDPS 2010), Apr 2010, Atlanta, United States.
            inria-00548899
    [2] Gene H. Golub and Charles F. Van Loan. 1996. Matrix Computations (3rd Ed.).

    Examples
    --------
    >>> a = ht.random.randn(9, 6, split=0)
    >>> qr = ht.linalg.qr(a)
    >>> print(ht.allclose(a, ht.dot(qr.Q, qr.R)))
    [0/1] True
    [1/1] True
    >>> st = torch.randn(9, 6)
    >>> a = ht.array(st, split=1)
    >>> a_comp = ht.array(st, split=0)
    >>> q, r = ht.linalg.qr(a)
    >>> print(ht.allclose(a_comp, ht.dot(q, r)))
    [0/1] True
    [1/1] True
    """
    if not isinstance(a, dndarray.DNDarray):
        raise TypeError("'a' must be a DNDarray")
    if not isinstance(tiles_per_proc, (int, torch.Tensor)):
        raise TypeError(
            "tiles_per_proc must be an int or a torch.Tensor, "
            "currently {}".format(type(tiles_per_proc))
        )
    if not isinstance(calc_q, bool):
        raise TypeError("calc_q must be a bool, currently {}".format(type(calc_q)))
    if not isinstance(overwrite_a, bool):
        raise TypeError("overwrite_a must be a bool, currently {}".format(type(overwrite_a)))
    if isinstance(tiles_per_proc, torch.Tensor):
        raise ValueError(
            "tiles_per_proc must be a single element torch.Tenor or int, "
            "currently has {} entries".format(tiles_per_proc.numel())
        )
    if len(a.shape) != 2:
        raise ValueError("Array 'a' must be 2 dimensional")

    QR = collections.namedtuple("QR", "Q, R")

    if a.split is None:
        q, r = a._DNDarray__array.qr(some=False)
        q = factories.array(q, device=a.device)
        r = factories.array(r, device=a.device)
        ret = QR(q if calc_q else None, r)
        return ret
    # =============================== Prep work ====================================================
    r = a if overwrite_a else a.copy()
    r.create_square_diag_tiles(tiles_per_proc=tiles_per_proc)
    tile_columns = r.tiles.tile_columns
    tile_rows = r.tiles.tile_rows
    if calc_q:
        q = factories.eye(
            (r.gshape[0], r.gshape[0]), split=0, dtype=r.dtype, comm=r.comm, device=r.device
        )
        q.create_square_diag_tiles(tiles_per_proc=tiles_per_proc)
        q.tiles.match_tiles(r.tiles)
    else:
        q = None
    # ==============================================================================================

    if a.split == 0:
        rank = r.comm.rank
        active_procs = torch.arange(r.comm.size, device=r.device.torch_device)
        empties = torch.nonzero(r.tiles.lshape_map[..., 0] == 0)
        empties = empties[0] if empties.numel() > 0 else []
        for e in empties:
            active_procs = active_procs[active_procs != e]
        tile_rows_per_pr_trmd = r.tiles.tile_rows_per_process[: active_procs[-1] + 1]

        q_dict = {}
        q_dict_waits = {}
        proc_tile_start = torch.cumsum(
            torch.tensor(tile_rows_per_pr_trmd, device=r.device.torch_device), dim=0
        )
        # ------------------------------------ R Calculation ---------------------------------------
        for col in range(
            tile_columns
        ):  # for each tile column (need to do the last rank separately)
            # for each process need to do local qr
            not_completed_processes = torch.nonzero(col < proc_tile_start).flatten()
            # print(col, torch.nonzero(col >= proc_tile_start).flatten())
            if rank not in not_completed_processes or rank not in active_procs:
                # if the process is done calculating R the break the loop
                break
            diag_process = not_completed_processes[0]
            __qr_split0_r_calc(
                r_tiles=r.tiles,
                q_dict=q_dict,
                q_dict_waits=q_dict_waits,
                col_num=col,
                diag_pr=diag_process,
                not_completed_prs=not_completed_processes,
            )
        # ------------------------------------- Q Calculation --------------------------------------
        for col in range(tile_columns):
            __qr_split0_q_loop(
                col=col,
                r=r,
                proc_tile_start=proc_tile_start,
                active_procs=active_procs,
                q0=q,
                q_dict=q_dict,
                q_dict_waits=q_dict_waits,
            )
    elif a.split == 1:
        # loop over the tile columns
        lp_cols = tile_columns if a.gshape[0] > a.gshape[1] else tile_rows
        for dcol in range(lp_cols):  # dcol is the diagonal column
            __qr_split1_loop(dcol=dcol, a=r, q0=q, calc_q=calc_q)

    r.balance_()
    if q is not None:
        q.balance_()

    ret = QR(q, r)
    return ret


def __qr_split0_global_q_dict_set(q_dict_col, col, r_tiles, q_tiles, global_merge_dict=None):
    """
    The function takes the orginial Q tensors from the global QR calculation and sets them to
    the keys which corresponds with their tile coordinates in Q. this returns a separate dictionary,
    it does NOT set the values of Q

    Parameters
    ----------
    q_dict_col : Dict
        The dictionary of the Q values for a given column, should be given as q_dict[col]
    col : int, single element torch.Tensor
        current column for which Q is being calculated for
    r_tiles : tiling.SquareDiagTiles
        tiling object for 'r'
    q_tiles : tiling.SquareDiagTiles
        tiling object for Q0
    global_merge_dict : Dict, optional
        the ouput of the function will be in this dictionary
        Form of output: key index : torch.Tensor

    Returns
    -------
    None
    """
    # q is already created, the job of this function is to create the group the merging q's together
    # it takes the merge qs, splits them, then puts them into a new dictionary
    # steps
    proc_tile_start = torch.cumsum(
        torch.tensor(r_tiles.tile_rows_per_process, device=r_tiles.arr._DNDarray__array.device),
        dim=0,
    )
    diag_proc = torch.nonzero(proc_tile_start > col)[0].item()
    proc_tile_start = torch.cat(
        (torch.tensor([0], device=r_tiles.arr._DNDarray__array.device), proc_tile_start[:-1]), dim=0
    )

    # 1: create caqr dictionary
    # need to have empty lists for all tiles in q
    global_merge_dict = {} if global_merge_dict is None else global_merge_dict

    # intended to be used as [row][column] -> data
    # 2: loop over keys in the dictionary
    merge_list = list(q_dict_col.keys())
    merge_list.sort()
    # todo: possible improvement -> make the keys have the process they are on as well,
    #  then can async get them if they are not on the diagonal process
    for key in merge_list:
        # print(col, key)
        # this loops over all of the Qs for col and creates the dictionary for the pr Q merges
        p0 = key.find("p0")
        p1 = key.find("p1")
        end = key.find("e")
        r0 = int(key[p0 + 2 : p1])
        r1 = int(key[p1 + 2 : end])
        lp_q = q_dict_col[key][0]
        base_size = q_dict_col[key][1]
        # cut the q into 4 bits (end of base array)
        # todo: modify this so that it will get what is needed from the process,
        #  instead of gathering all the qs
        top_left = lp_q[: base_size[0], : base_size[0]]
        top_right = lp_q[: base_size[0], base_size[0] :]
        bottom_left = lp_q[base_size[0] :, : base_size[0]]
        bottom_right = lp_q[base_size[0] :, base_size[0] :]
        # need to adjust the keys to be the global row
        if diag_proc == r0:
            col1 = col
        else:
            col1 = proc_tile_start[r0].item()
        col2 = proc_tile_start[r1].item()
        # col0 and col1 are the columns numbers
        # r0 and r1 are the ranks
        jdim = (col1, col1)
        kdim = (col1, col2)
        ldim = (col2, col1)
        mdim = (col2, col2)

        # if there are no elements on that location than set it as the tile
        # 1. get keys of what already has data
        curr_keys = set(global_merge_dict.keys())
        # 2. determine which tiles need to be touched/created
        # these are the keys which are to be multiplied by the q in the current loop
        # for matrix of form: | J  K |
        #                     | L  M |
        mult_keys_00 = [(i, col1) for i in range(q_tiles.tile_columns)]  # (J)
        # (J) -> inds: (i, col0)(col0, col0) -> set at (i, col0)
        mult_keys_01 = [(i, col1) for i in range(q_tiles.tile_columns)]  # (K)
        # (K) -> inds: (i, col0)(col0, col1) -> set at (i, col1)
        mult_keys_10 = [(i, col2) for i in range(q_tiles.tile_columns)]  # (L)
        # (L) -> inds: (i, col1)(col1, col0) -> set at (i, col0)
        mult_keys_11 = [(i, col2) for i in range(q_tiles.tile_columns)]  # (M)
        # (M) -> inds: (i, col1)(col1, col1) -> set at (i, col1)

        # if there are no elements in the mult_keys then set the element to the same place
        s00 = set(mult_keys_00) & curr_keys
        s01 = set(mult_keys_01) & curr_keys
        s10 = set(mult_keys_10) & curr_keys
        s11 = set(mult_keys_11) & curr_keys
        hold_dict = global_merge_dict.copy()

        # (J)
        if not len(s00):
            global_merge_dict[jdim] = top_left
        else:  # -> do the mm for all of the mult keys
            for k in s00:
                global_merge_dict[k[0], jdim[1]] = hold_dict[k] @ top_left
        # (K)
        if not len(s01):
            # check that we are not overwriting here
            global_merge_dict[kdim] = top_right
        else:  # -> do the mm for all of the mult keys
            for k in s01:
                global_merge_dict[k[0], kdim[1]] = hold_dict[k] @ top_right
        # (L)
        if not len(s10):
            # check that we are not overwriting here
            global_merge_dict[ldim] = bottom_left
        else:  # -> do the mm for all of the mult keys
            for k in s10:
                global_merge_dict[k[0], ldim[1]] = hold_dict[k] @ bottom_left
        # (M)
        if not len(s11):
            # check that we are not overwriting here
            global_merge_dict[mdim] = bottom_right
        else:  # -> do the mm for all of the mult keys
            for k in s11:
                global_merge_dict[k[0], mdim[1]] = hold_dict[k] @ bottom_right
    return global_merge_dict


def __qr_split0_r_calc(r_tiles, q_dict, q_dict_waits, col_num, diag_pr, not_completed_prs):
    """
    Function to do the QR calculations to calculate the global R of the array `a`.
    This function uses a binary merge structure in the globabl R merge.

    Parameters
    ----------
    r_tiles : tiling.SquareDiagTiles
        tiling object for 'r'
    q_dict : Dict
        dictionary to save the calculated Q matrices to
    q_dict_waits : Dict
        dictionary to save the calculated Q matrices to which are
        not calculated on the diagonal process
    col_num : int
        the current column of the the R calculation
    diag_pr : int
        rank of the process which has the tile which lies along the diagonal
    not_completed_prs : torch.Tensor
        tensor of the processes which have not yet finished calculating R

    Returns
    -------
    None
    """
    tile_rows_proc = r_tiles.tile_rows_per_process
    comm = r_tiles.arr.comm
    rank = comm.rank
    lcl_tile_row = 0 if rank != diag_pr else col_num - sum(tile_rows_proc[:rank])
    # only work on the processes which have not computed the final result
    q_dict[col_num] = {}
    q_dict_waits[col_num] = {}

    # --------------- local QR calc -----------------------------------------------------
    base_tile = r_tiles.local_get(key=(slice(lcl_tile_row, None), col_num))
    q1, r1 = base_tile.qr(some=False)
    q_dict[col_num]["l0"] = [q1, base_tile.shape]
    r_tiles.local_set(key=(slice(lcl_tile_row, None), col_num), value=r1)
    if col_num != r_tiles.tile_columns - 1:
        base_rest = r_tiles.local_get((slice(lcl_tile_row, None), slice(col_num + 1, None)))
        loc_rest = torch.matmul(q1.T, base_rest)
        r_tiles.local_set(key=(slice(lcl_tile_row, None), slice(col_num + 1, None)), value=loc_rest)
    # --------------- global QR calc (binary merge) -------------------------------------
    rem1 = None
    rem2 = None
    offset = not_completed_prs[0]
    loop_size_remaining = not_completed_prs.clone()
    completed = False if loop_size_remaining.size()[0] > 1 else True
    procs_remaining = loop_size_remaining.size()[0]
    loop = 0
    while not completed:
        # print(procs_remaining, loop_size_remaining)
        if procs_remaining % 2 == 1:
            # if the number of processes active is odd need to save the remainders
            if rem1 is None:
                rem1 = loop_size_remaining[-1]
                loop_size_remaining = loop_size_remaining[:-1]
            elif rem2 is None:
                rem2 = loop_size_remaining[-1]
                loop_size_remaining = loop_size_remaining[:-1]
        if rank not in loop_size_remaining and rank not in [rem1, rem2]:
            break  # if the rank is done then exit the loop
        # send the data to the corresponding processes
        zipped = zip(
            loop_size_remaining.flatten()[: procs_remaining // 2],
            loop_size_remaining.flatten()[procs_remaining // 2 :],
        )
        for pr in zipped:
            pr0, pr1 = int(pr[0].item()), int(pr[1].item())
            __qr_split0_merge_tile_rows(
                pr0=pr0,
                pr1=pr1,
                column=col_num,
                rank=rank,
                r_tiles=r_tiles,
                diag_process=diag_pr,
                key=str(loop) + "p0" + str(pr0) + "p1" + str(pr1) + "e",
                q_dict=q_dict,
            )

            __qr_split0_send_q_to_diag_pr(
                col=col_num,
                pr0=pr0,
                pr1=pr1,
                diag_process=diag_pr,
                comm=comm,
                q_dict=q_dict,
                key=str(loop) + "p0" + str(pr0) + "p1" + str(pr1) + "e",
                q_dict_waits=q_dict_waits,
                q_dtype=r_tiles.arr.dtype.torch_type(),
                q_device=r_tiles.arr._DNDarray__array.device,
            )

        loop_size_remaining = loop_size_remaining[: -1 * (procs_remaining // 2)]
        procs_remaining = loop_size_remaining.size()[0]

        if rem1 is not None and rem2 is not None:
            # combine rem1 and rem2 in the same way as the other nodes,
            # then save the results in rem1 to be used later
            __qr_split0_merge_tile_rows(
                pr0=rem2,
                pr1=rem1,
                column=col_num,
                rank=rank,
                r_tiles=r_tiles,
                diag_process=diag_pr,
                key=str(loop) + "p0" + str(int(rem1)) + "p1" + str(int(rem2)) + "e",
                q_dict=q_dict if q_dict is not None else {},
            )

            rem1, rem2 = int(rem1), int(rem2)
            __qr_split0_send_q_to_diag_pr(
                col=col_num,
                pr0=rem2,
                pr1=rem1,
                diag_process=diag_pr,
                key=str(loop) + "p0" + str(int(rem1)) + "p1" + str(int(rem2)) + "e",
                q_dict=q_dict if q_dict is not None else {},
                comm=comm,
                q_dict_waits=q_dict_waits,
                q_dtype=r_tiles.arr.dtype.torch_type(),
                q_device=r_tiles.arr._DNDarray__array.device,
            )
            rem1 = rem2
            rem2 = None

        loop += 1
        if rem1 is not None and rem2 is None and procs_remaining == 1:
            # combine rem1 with process 0 (offset) and set completed to True
            # this should be the last thing that happens
            __qr_split0_merge_tile_rows(
                pr0=offset,
                pr1=rem1,
                column=col_num,
                rank=rank,
                r_tiles=r_tiles,
                diag_process=diag_pr,
                key=str(loop) + "p0" + str(int(offset)) + "p1" + str(int(rem1)) + "e",
                q_dict=q_dict,
            )

            offset, rem1 = int(offset), int(rem1)
            __qr_split0_send_q_to_diag_pr(
                col=col_num,
                pr0=offset,
                pr1=rem1,
                diag_process=diag_pr,
                key=str(loop) + "p0" + str(int(offset)) + "p1" + str(int(rem1)) + "e",
                q_dict=q_dict,
                comm=comm,
                q_dict_waits=q_dict_waits,
                q_dtype=r_tiles.arr.dtype.torch_type(),
                q_device=r_tiles.arr._DNDarray__array.device,
            )
            rem1 = None

        completed = True if procs_remaining == 1 and rem1 is None and rem2 is None else False


def __qr_split0_local_q_calc(r_tiles, q0_tiles, col, q_dict, diag_process, active_procs):
    """
    Does the local Q calculation for the QR of a split=0 DNDarray.

    Parameters
    ----------
    r_tiles : tiling.SquareDiagTiles
        tiling object for 'a'
    q0_tiles : tiling.SquareDiagTiles
        tiling object for Q0
    col : int
        the current column of the the R calculation
    q_dict : Dict
        dictionary to save the calculated Q matrices to
    diag_process : int
        rank of the process which has the tile which lies along the diagonal
    active_procs : torch.Tensor
        tensor containing the processes which have not yet finished calculating Q

    Returns
    -------
    None
    """
    rank = r_tiles.arr.comm.rank
    a_torch_device = r_tiles.arr.device.torch_device
    if col in q_dict.keys():
        lcl_col_shape = r_tiles.local_get(key=(slice(None), col)).shape
        # get the start and stop of all local tiles
        #   -> get the rows_per_process[rank] and the row_indices
        row_ind = r_tiles.row_indices
        prev_rows_per_pr = sum(r_tiles.tile_rows_per_process[:rank])
        rows_per_pr = r_tiles.tile_rows_per_process[rank]
        if rows_per_pr == 1:
            # if there is only one tile on the process: return q_dict[col]['0']
            base_q = q_dict[col]["l0"][0].clone()
            del q_dict[col]["l0"]
        else:
            # 0. get the offset of the column start
            offset = (
                torch.tensor(
                    row_ind[col].item() - row_ind[prev_rows_per_pr].item(), device=a_torch_device
                )
                if row_ind[col].item() > row_ind[prev_rows_per_pr].item()
                else torch.tensor(0, device=a_torch_device)
            )
            # 1: create an eye matrix of the row's zero'th dim^2
            q_lcl = q_dict[col]["l0"]  # [0] -> q, [1] -> shape of a use in q calc (q is square)
            del q_dict[col]["l0"]
            base_q = torch.eye(
                lcl_col_shape[r_tiles.arr.split], dtype=q_lcl[0].dtype, device=a_torch_device
            )
            # 2: set the area of the eye as Q
            base_q[offset : offset + q_lcl[1][0], offset : offset + q_lcl[1][0]] = q_lcl[0]

        local_merge_q = {rank: [base_q, None]}
    else:
        local_merge_q = {}
    # -------------- send local Q to all -------------------------------------------------------
    q0_dtype = q0_tiles.arr.dtype
    q0_torch_type = q0_dtype.torch_type()
    q0_torch_device = q0_tiles.arr.device.torch_device
    for r in range(diag_process, active_procs[-1] + 1):
        if r != rank:
            hld = torch.zeros(
                [q0_tiles.lshape_map[r][q0_tiles.arr.split]] * 2,
                dtype=q0_torch_type,
                device=q0_torch_device,
            )
        else:
            hld = local_merge_q[r][0].clone()
        wait = q0_tiles.arr.comm.Ibcast(hld, root=r)
        local_merge_q[r] = [hld, wait]

    # recv local Q + apply local Q to Q0
    for r in range(diag_process, active_procs[-1] + 1):
        if local_merge_q[r][1] is not None:
            # receive q from the other processes
            local_merge_q[r][1].wait()
        if rank in active_procs:
            sum_row = sum(q0_tiles.tile_rows_per_process[:r])
            end_row = q0_tiles.tile_rows_per_process[r] + sum_row
            # slice of q_tiles -> [0: -> end local, 1: start -> stop]
            q_rest_loc = q0_tiles.local_get(key=(slice(None), slice(sum_row, end_row)))
            # apply the local merge to q0 then update q0`
            q_rest_loc = q_rest_loc @ local_merge_q[r][0]
            q0_tiles.local_set(key=(slice(None), slice(sum_row, end_row)), value=q_rest_loc)
            del local_merge_q[r]


def __qr_split0_merge_tile_rows(pr0, pr1, column, rank, r_tiles, diag_process, key, q_dict):
    """
    Merge two tile rows, take their QR, and apply it to the trailing process
    This will modify 'a' and set the value of the q_dict[column][key]
    with [Q, upper.shape, lower.shape].

    Parameters
    ----------
    pr0, pr1 : int, int
        Process ranks of the processes to be used
    column : int
        the current process of the QR calculation
    rank : int
        the rank of the process
    r_tiles : ht.tiles.SquareDiagTiles
        tiling object used for getting/setting the tiles required
    diag_process : int
        The rank of the process which has the tile along the diagonal for the given column

    Returns
    -------
    None, sets the value of q_dict[column][key] with [Q, upper.shape, lower.shape]
    """
    if rank not in [pr0, pr1]:
        return
    pr0 = pr0.item() if isinstance(pr0, torch.Tensor) else pr0
    pr1 = pr1.item() if isinstance(pr1, torch.Tensor) else pr1
    comm = r_tiles.arr.comm
    upper_row = sum(r_tiles.tile_rows_per_process[:pr0]) if pr0 != diag_process else column
    lower_row = sum(r_tiles.tile_rows_per_process[:pr1]) if pr1 != diag_process else column

    upper_inds = r_tiles.get_start_stop(key=(upper_row, column))
    lower_inds = r_tiles.get_start_stop(key=(lower_row, column))

    upper_size = (upper_inds[1] - upper_inds[0], upper_inds[3] - upper_inds[2])
    lower_size = (lower_inds[1] - lower_inds[0], lower_inds[3] - lower_inds[2])

    a_torch_device = r_tiles.arr._DNDarray__array.device

    # upper adjustments
    if upper_size[0] < upper_size[1] and r_tiles.tile_rows_per_process[pr0] > 1:
        # end of dim0 (upper_inds[1]) is equal to the size in dim1
        upper_inds = list(upper_inds)
        upper_inds[1] = upper_inds[0] + upper_size[1]
        upper_size = (upper_inds[1] - upper_inds[0], upper_inds[3] - upper_inds[2])
    if lower_size[0] < lower_size[1] and r_tiles.tile_rows_per_process[pr1] > 1:
        # end of dim0 (upper_inds[1]) is equal to the size in dim1
        lower_inds = list(lower_inds)
        lower_inds[1] = lower_inds[0] + lower_size[1]
        lower_size = (lower_inds[1] - lower_inds[0], lower_inds[3] - lower_inds[2])

    if rank == pr0:
        # need to use lloc on r_tiles.arr with the indices
        upper = r_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[2] : upper_inds[3]]

        comm.Send(upper.clone(), dest=pr1, tag=986)
        lower = torch.zeros(lower_size, dtype=r_tiles.arr.dtype.torch_type(), device=a_torch_device)
        comm.Recv(lower, source=pr1, tag=4363)
    else:  # rank == pr1:
        lower = r_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[2] : lower_inds[3]]
        upper = torch.zeros(upper_size, dtype=r_tiles.arr.dtype.torch_type(), device=a_torch_device)
        comm.Recv(upper, source=pr0, tag=986)
        comm.Send(lower.clone(), dest=pr0, tag=4363)

    q_merge, r = torch.cat((upper, lower), dim=0).qr(some=False)
    upp = r[: upper.shape[0]]
    low = r[upper.shape[0] :]
    if rank == pr0:
        r_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[2] : upper_inds[3]] = upp
    else:  # rank == pr1:
        r_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[2] : lower_inds[3]] = low

    if column < r_tiles.tile_columns - 1:
        upper_rest_size = (upper_size[0], r_tiles.arr.gshape[1] - upper_inds[3])
        lower_rest_size = (lower_size[0], r_tiles.arr.gshape[1] - lower_inds[3])

        if rank == pr0:
            upper_rest = r_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[3] :]
            lower_rest = torch.zeros(
                lower_rest_size, dtype=r_tiles.arr.dtype.torch_type(), device=a_torch_device
            )
            comm.Send(upper_rest.clone(), dest=pr1, tag=98654)
            comm.Recv(lower_rest, source=pr1, tag=436364)
        else:  # rank == pr1:
            lower_rest = r_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[3] :]
            upper_rest = torch.zeros(
                upper_rest_size, dtype=r_tiles.arr.dtype.torch_type(), device=a_torch_device
            )
            comm.Recv(upper_rest, source=pr0, tag=98654)
            comm.Send(lower_rest.clone(), dest=pr0, tag=436364)

        cat_tensor = torch.cat((upper_rest, lower_rest), dim=0)
        new_rest = torch.matmul(q_merge.t(), cat_tensor)
        # the data for upper rest is a slice of the new_rest, need to slice only the 0th dim
        upp = new_rest[: upper_rest.shape[0]]
        low = new_rest[upper_rest.shape[0] :]
        if rank == pr0:
            r_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[3] :] = upp
        # set the lower rest
        else:  # rank == pr1:
            r_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[3] :] = low

    q_dict[column][key] = [q_merge, upper.shape, lower.shape]


def __qr_split0_send_q_to_diag_pr(
    col, pr0, pr1, diag_process, comm, q_dict, key, q_dict_waits, q_dtype, q_device
):
    """
    This function sends the merged Q to the diagonal process. Buffered send it used for sending
    Q. This is needed for the Q calculation when two processes are merged and neither is the diagonal
    process.

    Parameters
    ----------
    col : int
        The current column used in the parent QR loop
    pr0, pr1 : int, int
        Rank of processes 0 and 1. These are the processes used in the calculation of q
    diag_process : int
        The rank of the process which has the tile along the diagonal for the given column
    comm : MPICommunication (ht.DNDarray.comm)
        The communicator used. (Intended as the communication of the DNDarray 'a' given to qr)
    q_dict : Dict
        dictionary containing the Q values calculated for finding R
    key : string
        key for q_dict[col] which corresponds to the Q to send
    q_dict_waits : Dict
        Dictionary used in the collection of the Qs which are sent to the diagonal process
    q_dtype : torch.type
        Type of the Q tensor
    q_device : torch.Device
        Device of the Q tensor

    Returns
    -------
    None, sets the values of q_dict_waits with the with *waits* for the values of Q, upper.shape,
        and lower.shape
    """
    if comm.rank not in [pr0, pr1, diag_process]:
        return
    # this is to send the merged q to the diagonal process for the forming of q
    base_tag = "1" + str(pr1.item() if isinstance(pr1, torch.Tensor) else pr1)
    if comm.rank == pr1:
        q = q_dict[col][key][0]
        u_shape = q_dict[col][key][1]
        l_shape = q_dict[col][key][2]
        comm.send(tuple(q.shape), dest=diag_process, tag=int(base_tag + "1"))
        comm.Isend(q, dest=diag_process, tag=int(base_tag + "12"))
        comm.send(u_shape, dest=diag_process, tag=int(base_tag + "123"))
        comm.send(l_shape, dest=diag_process, tag=int(base_tag + "1234"))
    if comm.rank == diag_process:
        # q_dict_waits now looks like a
        q_sh = comm.recv(source=pr1, tag=int(base_tag + "1"))
        q_recv = torch.zeros(q_sh, dtype=q_dtype, device=q_device)
        k = "p0" + str(pr0) + "p1" + str(pr1)
        q_dict_waits[col][k] = []
        q_wait = comm.Irecv(q_recv, source=pr1, tag=int(base_tag + "12"))
        q_dict_waits[col][k].append([q_recv, q_wait])
        q_dict_waits[col][k].append(comm.irecv(source=pr1, tag=int(base_tag + "123")))
        q_dict_waits[col][k].append(comm.irecv(source=pr1, tag=int(base_tag + "1234")))
        q_dict_waits[col][k].append(key[0])


def __qr_split0_q_loop(col, r, proc_tile_start, active_procs, q0, q_dict, q_dict_waits):
    """
    Function for Calculating Q for split=0 for QR. col is the index of the tile column. The
    assumption here is that the diagonal tile is (col, col).

    Parameters
    ----------
    col : int
        current column for which to calculate Q
    r : DNDarray
        the R array
    proc_tile_start : torch.Tensor
        Tensor containing the row tile start indices for each process
    active_procs : torch.Tensor
        Tensor containing the ranks of processes with have data
    q0 : DNDarray
        the Q array
    q_dict : Dictionary
        Dictionary created in the split=0 R calculation containing all of the Q matrices found
        transforming the matrix to upper triangular for each column. The keys of this dictionary are
        the column indices
    q_dict_waits : Dictionary
        Dictionary created while sending the Q matrices to the diagonal process

    Returns
    -------
    None
    """
    tile_columns = r.tiles.tile_columns
    diag_process = (
        torch.nonzero(proc_tile_start > col)[0] if col != tile_columns else proc_tile_start[-1]
    )
    diag_process = diag_process.item()
    rank = r.comm.rank
    q0_torch_device = q0._DNDarray__array.device
    # wait for Q tensors sent during the R calculation -----------------------------------------
    if col in q_dict_waits.keys():
        for key in q_dict_waits[col].keys():
            new_key = q_dict_waits[col][key][3] + key + "e"
            q_dict_waits[col][key][0][1].wait()
            q_dict[col][new_key] = [
                q_dict_waits[col][key][0][0],
                q_dict_waits[col][key][1].wait(),
                q_dict_waits[col][key][2].wait(),
            ]
        del q_dict_waits[col]
    # local Q calculation ----------------------------------------------------------------------
    __qr_split0_local_q_calc(
        r_tiles=r.tiles,
        q0_tiles=q0.tiles,
        col=col,
        q_dict=q_dict,
        diag_process=diag_process,
        active_procs=active_procs,
    )

    # global Q calculation ---------------------------------------------------------------------
    # split up the Q's from the global QR calculation and set them in a dict w/ proper keys
    global_merge_dict = (
        __qr_split0_global_q_dict_set(
            q_dict_col=q_dict[col], col=col, r_tiles=r.tiles, q_tiles=q0.tiles
        )
        if rank == diag_process
        else {}
    )

    if rank == diag_process:
        merge_dict_keys = set(global_merge_dict.keys())
    else:
        merge_dict_keys = None
    merge_dict_keys = r.comm.bcast(merge_dict_keys, root=diag_process)

    # send the global merge dictionary to all processes
    for k in merge_dict_keys:
        if rank == diag_process:
            snd = global_merge_dict[k].clone()
            snd_shape = snd.shape
            r.comm.bcast(snd_shape, root=diag_process)
        else:
            snd_shape = None
            snd_shape = r.comm.bcast(snd_shape, root=diag_process)
            snd = torch.empty(snd_shape, dtype=q0.dtype.torch_type(), device=q0_torch_device)

        wait = r.comm.Ibcast(snd, root=diag_process)
        global_merge_dict[k] = [snd, wait]
    if rank in active_procs:
        # create a dictionary which says what tiles are in each column of the global merge Q
        qi_mult = {}
        for c in range(q0.tiles.tile_columns):
            # this loop is to slice the merge_dict keys along each column + create the
            qi_mult_set = set([(i, c) for i in range(col, q0.tiles.tile_columns)])
            if len(qi_mult_set & merge_dict_keys) != 0:
                qi_mult[c] = list(qi_mult_set & merge_dict_keys)

        # have all the q_merge in one place, now just do the mm with q0
        # get all the keys which are in a column (qi_mult[column])
        row_inds = q0.tiles.row_indices + [q0.tiles.arr.gshape[0]]
        q_copy = q0.tiles.arr._DNDarray__array.clone()
        for qi_col in qi_mult.keys():
            # multiply q0 rows with qi cols
            # the result of this will take the place of the row height and the column width
            out_sz = q0.tiles.local_get(key=(slice(None), qi_col)).shape
            mult_qi_col = torch.zeros(
                (q_copy.shape[1], out_sz[1]), dtype=q0.dtype.torch_type(), device=q0_torch_device
            )
            for ind in qi_mult[qi_col]:
                if global_merge_dict[ind][1] is not None:
                    global_merge_dict[ind][1].wait()
                lp_q = global_merge_dict[ind][0]
                if mult_qi_col.shape[1] < lp_q.shape[1]:
                    new_mult = torch.zeros(
                        (mult_qi_col.shape[0], lp_q.shape[1]),
                        dtype=mult_qi_col.dtype,
                        device=q0_torch_device,
                    )
                    new_mult[:, : mult_qi_col.shape[1]] += mult_qi_col.clone()
                    mult_qi_col = new_mult

                mult_qi_col[
                    row_inds[ind[0]] : row_inds[ind[0]] + lp_q.shape[0], : lp_q.shape[1]
                ] = lp_q
            hold = torch.matmul(q_copy, mult_qi_col)

            write_inds = q0.tiles.get_start_stop(key=(0, qi_col))
            q0.tiles.arr.lloc[:, write_inds[2] : write_inds[2] + hold.shape[1]] = hold
    else:
        for ind in merge_dict_keys:
            global_merge_dict[ind][1].wait()
    if col in q_dict.keys():
        del q_dict[col]


def __qr_split1_loop(dcol, a, q0, calc_q):
    """
    Helper function to do the QR factorization of the column 'dcol'. This function assumes that the
    target tile is at (dcol, dcol). This is the standard case at it assumes that the diagonal tile
    holds the diagonal entries of the matrix.

    Parameters
    ----------
    dcol : int
        column of the diagonal process
    a : DNDarray
        input matrix to QR, if copy is true in QR then it is a copy of the data, else it is the
        same as the input
    q0 : DNDarray
        the Q matrix as created in the QR function.
    calc_q : Boolean
        Flag for weather to calculate Q or not, if False, then Q=None

    Returns
    -------
    None
    """
    a_torch_device = a._DNDarray__array.device
    q0_torch_device = q0._DNDarray__array.device
    # ==================================== R Calculation - single tile =========================
    # loop over each column, need to do the QR for each tile in the column(should be rows)
    # need to get the diagonal process
    rank = a.comm.rank
    cols_on_proc = torch.cumsum(
        torch.tensor(a.tiles.tile_columns_per_process, device=a_torch_device), dim=0
    )
    not_completed_processes = torch.nonzero(dcol < cols_on_proc).flatten()
    diag_process = not_completed_processes[0].item()
    tile_rows = a.tiles.tile_rows
    # get the diagonal tile and do qr on it
    # send q to the other processes
    # 1st qr: only on diagonal tile + apply to the row
    if rank == diag_process:
        # do qr on diagonal process
        q1, r1 = a.tiles[dcol, dcol].qr(some=False)
        a.comm.Bcast(q1.clone(), root=diag_process)
        a.tiles[dcol, dcol] = r1
        # apply q1 to the trailing matrix (other processes)

        # need to convert dcol to a local index
        loc_col = dcol - sum(a.tiles.tile_columns_per_process[:rank])
        hold = a.tiles.local_get(key=(dcol, slice(loc_col + 1, None)))
        if hold is not None:  # if there is more data on that row after the diagonal tile
            a.tiles.local_set(key=(dcol, slice(loc_col + 1, None)), value=torch.matmul(q1.T, hold))
    elif rank > diag_process:
        # recv the Q from the diagonal process, and apply it to the trailing matrix
        st_sp = a.tiles.get_start_stop(key=(dcol, dcol))
        sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]

        q1 = torch.zeros((sz[0], sz[0]), dtype=a.dtype.torch_type(), device=a_torch_device)
        loc_col = 0
        a.comm.Bcast(q1, root=diag_process)
        hold = a.tiles.local_get(key=(dcol, slice(0, None)))
        a.tiles.local_set(key=(dcol, slice(0, None)), value=torch.matmul(q1.T, hold))
    else:
        # these processes are already done calculating R, only need to calc Q, need to recv q1
        st_sp = a.tiles.get_start_stop(key=(dcol, dcol))
        sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
        q1 = torch.zeros((sz[0], sz[0]), dtype=a.dtype.torch_type(), device=a_torch_device)
        a.comm.Bcast(q1, root=diag_process)

    # ================================ Q Calculation - single tile =============================
    if calc_q:
        for row in range(q0.tiles.tile_rows_per_process[rank]):
            # q1 is applied to each tile of the column dcol of q0 then written there
            q0.tiles.local_set(
                key=(row, dcol), value=torch.matmul(q0.tiles.local_get(key=(row, dcol)), q1)
            )
    del q1
    # loop over the rest of the rows, combine the tiles, then apply the result to the rest
    # 2nd step: merged QR on the rows
    # ================================ R Calculation - merged tiles ============================
    diag_tile = a.tiles[dcol, dcol]
    st_sp = a.tiles.get_start_stop(key=(dcol, dcol))
    diag_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
    # (Q) need to get the start stop of diag tial
    diag_st_sp = a.tiles.get_start_stop(key=(dcol, dcol))
    for row in range(dcol + 1, tile_rows):
        if rank == diag_process:
            # cat diag tile and loop tile
            loop_tile = a.tiles[row, dcol]
            loop_cat = torch.cat((diag_tile, loop_tile), dim=0)
            # qr
            ql, rl = loop_cat.qr(some=False)
            # send ql to all
            a.comm.Bcast(ql.clone(), root=diag_process)
            # set rs
            a.tiles[dcol, dcol] = rl[: diag_sz[0]]
            a.tiles[row, dcol] = rl[diag_sz[0] :]
            # apply q to rest
            if loc_col + 1 < a.tiles.tile_columns_per_process[rank]:
                upp = a.tiles.local_get(key=(dcol, slice(loc_col + 1, None)))
                low = a.tiles.local_get(key=(row, slice(loc_col + 1, None)))
                hold = torch.matmul(ql.T, torch.cat((upp, low), dim=0))
                # set upper
                a.tiles.local_set(key=(dcol, slice(loc_col + 1, None)), value=hold[: diag_sz[0]])
                # set lower
                a.tiles.local_set(key=(row, slice(loc_col + 1, None)), value=hold[diag_sz[0] :])
        elif rank > diag_process:
            st_sp = a.tiles.get_start_stop(key=(row, dcol))
            lp_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            ql = torch.zeros(
                [lp_sz[0] + diag_sz[0]] * 2, dtype=a.dtype.torch_type(), device=a_torch_device
            )
            a.comm.Bcast(ql, root=diag_process)
            upp = a.tiles.local_get(key=(dcol, slice(0, None)))
            low = a.tiles.local_get(key=(row, slice(0, None)))
            hold = torch.matmul(ql.T, torch.cat((upp, low), dim=0))
            # set upper
            a.tiles.local_set(key=(dcol, slice(0, None)), value=hold[: diag_sz[0]])
            # set lower
            a.tiles.local_set(key=(row, slice(0, None)), value=hold[diag_sz[0] :])
        else:
            st_sp = a.tiles.get_start_stop(key=(row, dcol))
            lp_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            ql = torch.zeros(
                [lp_sz[0] + diag_sz[0]] * 2, dtype=a.dtype.torch_type(), device=a_torch_device
            )
            a.comm.Bcast(ql, root=diag_process)
        # ================================ Q Calculation - merged tiles ========================
        if calc_q:
            top_left = ql[: diag_sz[0], : diag_sz[0]]
            top_right = ql[: diag_sz[0], diag_sz[0] :]
            bottom_left = ql[diag_sz[0] :, : diag_sz[0]]
            bottom_right = ql[diag_sz[0] :, diag_sz[0] :]
            # two multiplications: one for the left tiles and one for the right
            # left tiles --------------------------------------------------------------------
            # create a column of the same size as the tile row of q0
            st_sp = a.tiles.get_start_stop(key=(slice(dcol, None), dcol))
            qloop_col_left_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            qloop_col_left = torch.zeros(
                qloop_col_left_sz, dtype=q0.dtype.torch_type(), device=q0_torch_device
            )
            # top left starts at 0 and goes until diag_sz[1]
            qloop_col_left[: diag_sz[0]] = top_left
            # bottom left starts at ? and goes until ? (only care about 0th dim)
            st, sp, _, _ = a.tiles.get_start_stop(key=(row, 0))
            st -= diag_st_sp[0]  # adjust these by subtracting the start index of the diag tile
            sp -= diag_st_sp[0]
            qloop_col_left[st:sp] = bottom_left
            # right tiles --------------------------------------------------------------------
            # create a columns tensor of the size of the tile column of index 'row'
            st_sp = q0.tiles.get_start_stop(key=(row, slice(dcol, None)))
            sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            qloop_col_right = torch.zeros(
                sz[1], sz[0], dtype=q0.dtype.torch_type(), device=q0_torch_device
            )
            # top left starts at 0 and goes until diag_sz[1]
            qloop_col_right[: diag_sz[0]] = top_right
            # bottom left starts at ? and goes until ? (only care about 0th dim)
            st, sp, _, _ = a.tiles.get_start_stop(key=(row, 0))
            st -= diag_st_sp[0]  # adjust these by subtracting the start index of the diag tile
            sp -= diag_st_sp[0]
            qloop_col_right[st:sp] = bottom_right
            for qrow in range(q0.tiles.tile_rows_per_process[rank]):
                # q1 is applied to each tile of the column dcol of q0 then written there
                q0_row = q0.tiles.local_get(key=(qrow, slice(dcol, None))).clone()
                q0.tiles.local_set(key=(qrow, dcol), value=torch.matmul(q0_row, qloop_col_left))
                q0.tiles.local_set(key=(qrow, row), value=torch.matmul(q0_row, qloop_col_right))
        del ql


def transpose(a, axes=None):
    """
    Permute the dimensions of an array.

    Parameters
    ----------
    a : array_like
        Input array.
    axes : None or list of ints, optional
        By default, reverse the dimensions, otherwise permute the axes according to the values given

    Returns
    -------
    p : ht.DNDarray
        a with its axes permuted.
    """
    # type check the input tensor
    if not isinstance(a, dndarray.DNDarray):
        raise TypeError("a must be of type ht.DNDarray, but was {}".format(type(a)))

    # set default value for axes permutations
    dimensions = len(a.shape)
    if axes is None:
        axes = tuple(reversed(range(dimensions)))
    # if given, sanitize the input
    else:
        try:
            # convert to a list to allow index access
            axes = list(axes)
        except TypeError:
            raise ValueError("axes must be an iterable containing ints")

        if len(axes) != dimensions:
            raise ValueError("axes do not match tensor shape")
        for index, axis in enumerate(axes):
            if not isinstance(axis, int):
                raise TypeError("axis must be an integer, but was {}".format(type(axis)))
            elif axis < 0:
                axes[index] = axis + dimensions

    # infer the new split axis, it is the position of the split axis within the new axes permutation
    try:
        transposed_split = axes.index(a.split) if a.split is not None else None
    except ValueError:
        raise ValueError("axes do not match tensor shape")

    # try to rearrange the tensor and return a new transposed variant
    try:
        transposed_data = a._DNDarray__array.permute(*axes)
        transposed_shape = tuple(a.shape[axis] for axis in axes)

        return dndarray.DNDarray(
            transposed_data, transposed_shape, a.dtype, transposed_split, a.device, a.comm
        )
    # if not possible re- raise any torch exception as ValueError
    except (RuntimeError, IndexError) as exception:
        raise ValueError(str(exception))


# statically allocated index slices for non-iterable dimensions in triangular operations
__index_base = (slice(None), slice(None))


def __tri_op(m, k, op):
    """
    Generic implementation of triangle operations on tensors. It takes care of input sanitation and
    non-standard broadcast behavior of the 2D triangle-operators.

    Parameters
    ----------
    m : ht.DNDarray
        Input tensor for which to compute the triangle operator.
    k : int, optional
        Diagonal above which to apply the triangle operator, k<0 is below and k>0 is above.
    op : callable
        Implementation of the triangle operator.

    Returns
    -------
    triangle_tensor : ht.DNDarray
        DNDarray with the applied triangle operation

    Raises
    ------
    TypeError
        If the input is not a tensor or the diagonal offset cannot be converted to an integral value.
    """
    if not isinstance(m, dndarray.DNDarray):
        raise TypeError("Expected m to be a tensor but was {}".format(type(m)))

    try:
        k = int(k)
    except ValueError:
        raise TypeError("Expected k to be integral, but was {}".format(type(k)))

    # chunk the global shape of the tensor to obtain the offset compared to the other ranks
    offset, _, _ = m.comm.chunk(m.shape, m.split)
    dimensions = len(m.shape)

    # manually repeat the input for vectors
    if dimensions == 1:
        triangle = m._DNDarray__array.expand(m.shape[0], -1)
        if torch.numel(triangle > 0):
            triangle = op(triangle, k - offset)

        return dndarray.DNDarray(
            triangle,
            (m.shape[0], m.shape[0]),
            m.dtype,
            None if m.split is None else 1,
            m.device,
            m.comm,
        )

    original = m._DNDarray__array
    output = original.clone()

    # modify k to account for tensor splits
    if m.split is not None:
        if m.split + 1 == dimensions - 1:
            k += offset
        elif m.split == dimensions - 1:
            k -= offset

    # in case of two dimensions we can just forward the call to the callable
    if dimensions == 2:
        if torch.numel(original) > 0:
            op(original, k, out=output)
    # more than two dimensions: iterate over all but the last two to realize 2D broadcasting
    else:
        ranges = [range(elements) for elements in m.lshape[:-2]]
        for partial_index in itertools.product(*ranges):
            index = partial_index + __index_base
            op(original[index], k, out=output[index])

    return dndarray.DNDarray(output, m.shape, m.dtype, m.split, m.device, m.comm)


def tril(m, k=0):
    """
    Returns the lower triangular part of the tensor, the other elements of the result tensor are set to 0.

    The lower triangular part of the tensor is defined as the elements on and below the diagonal.

    The argument k controls which diagonal to consider. If k=0, all elements on and below the main diagonal are
    retained. A positive value includes just as many diagonals above the main diagonal, and similarly a negative
    value excludes just as many diagonals below the main diagonal.

    Parameters
    ----------
    m : ht.DNDarray
        Input tensor for which to compute the lower triangle.
    k : int, optional
        Diagonal above which to zero elements. k=0 (default) is the main diagonal, k<0 is below and k>0 is above.

    Returns
    -------
    lower_triangle : ht.DNDarray
        Lower triangle of the input tensor.
    """
    return __tri_op(m, k, torch.tril)


def triu(m, k=0):
    """
    Returns the upper triangular part of the tensor, the other elements of the result tensor are set to 0.

    The upper triangular part of the tensor is defined as the elements on and below the diagonal.

    The argument k controls which diagonal to consider. If k=0, all elements on and below the main diagonal are
    retained. A positive value includes just as many diagonals above the main diagonal, and similarly a negative
    value excludes just as many diagonals below the main diagonal.

    Parameters
    ----------
    m : ht.DNDarray
        Input tensor for which to compute the upper triangle.
    k : int, optional
        Diagonal above which to zero elements. k=0 (default) is the main diagonal, k<0 is below and k>0 is above.

    Returns
    -------
    upper_triangle : ht.DNDarray
        Upper triangle of the input tensor.
    """
    return __tri_op(m, k, torch.triu)
