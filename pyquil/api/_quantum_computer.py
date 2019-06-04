##############################################################################
# Copyright 2018 Rigetti Computing
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
##############################################################################
import re
import warnings
from math import pi
from typing import List, Dict, Tuple, Iterator, Union
import itertools

import subprocess
from contextlib import contextmanager

import networkx as nx
import numpy as np
from scipy.linalg import hadamard
from rpcq.messages import BinaryExecutableResponse, PyQuilExecutableResponse

from pyquil.api._compiler import QPUCompiler, QVMCompiler
from pyquil.api._config import PyquilConfig
from pyquil.api._devices import get_lattice, list_lattices
from pyquil.api._error_reporting import _record_call
from pyquil.api._qac import AbstractCompiler
from pyquil.api._qam import QAM
from pyquil.api._qpu import QPU
from pyquil.api._qvm import ForestConnection, QVM
from pyquil.device import AbstractDevice, NxDevice, gates_in_isa, ISA, Device
from pyquil.gates import RX, MEASURE
from pyquil.noise import decoherence_noise_with_asymmetric_ro, NoiseModel
from pyquil.pyqvm import PyQVM
from pyquil.quil import Program, validate_supported_quil
from pyquil.quilbase import Measurement, Pragma

pyquil_config = PyquilConfig()

Executable = Union[BinaryExecutableResponse, PyQuilExecutableResponse]


def _get_flipped_protoquil_program(program: Program) -> Program:
    """For symmetrization, generate a program where X gates are added before measurement.

    Forest is picky about where the measure instructions happen. It has to be at the end!
    """
    program = program.copy()
    to_measure = []
    while len(program) > 0:
        inst = program.instructions[-1]
        if isinstance(inst, Measurement):
            program.pop()
            to_measure.append((inst.qubit, inst.classical_reg))
        else:
            break

    program += Pragma('PRESERVE_BLOCK')
    for qu, addr in to_measure[::-1]:
        program += RX(pi, qu)
    program += Pragma('END_PRESERVE_BLOCK')

    for qu, addr in to_measure[::-1]:
        program += Measurement(qubit=qu, classical_reg=addr)

    return program

def _flip_array_to_prog(flip_array: Tuple[bool], qubits: List[int]) -> Program:
    """
    Generate a pre-measurement program that flips the qubit state according to the flip_array of
    bools.

    This is used, for example, in exhaustive_symmetrization to produce programs which flip a
    select subset of qubits immediately before measurement.

    :param flip_array: tuple of booleans specifying whether the qubit in the corresponding index
        should be flipped or not.
    :param qubits: list specifying the qubits in order corresponding to the flip_array
    :return: Program which flips each qubit (i.e. instructs RX(pi, q)) according to the flip_array.
    """
    assert len(flip_array) == len(qubits), "Mismatch of qubits and operations"
    prog = Program()
    for qubit, flip_output in zip(qubits, flip_array):
        if flip_output == 0:
            continue
        elif flip_output == 1:
            prog += Program(RX(pi, qubit))
        else:
            raise ValueError("flip_bools should only consist of 0s and/or 1s")
    return prog

# ==================================================================================================
# Between this line ^^ and the one below this code is used to test...will delete

def two_qubit_bit_flip_operators(p00, p01, p10, p11):
    """
    Return a special case of a two qubit asymmetric bit flip kraus operators.

    Suppose we prepare a two qubit state |i,j> = |i>\otimes|j> for i,j \in {0,1}.

    Then pij := Pr(measured=ij|prepared=ij). So if pij = 1 no flip happens.

    For example consider p00 = 1-epsilon then a flip happens with probablity epsilon.
    The flip is symmetrically superposed over flipping to the states |0,1>, |1,0>, and |1,1>.
    The asymmetry comes from the fact that p00 does not have to be equal to p10 etc.

    :param p00: the probablity of |0,0> to remain in |0,0>
    :param p01: the probablity of |0,1> to remain in |0,1>
    :param p10: the probablity of |1,0> to remain in |1,0>
    :param p11: the probablity of |1,1> to remain in |1,1>
    :returns: a list of four Kraus operators.
    """
    p00e = 1.0 - p00
    p01e = 1.0 - p01
    p10e = 1.0 - p10
    p11e = 1.0 - p11
    kI = np.array([[np.sqrt(1 - p00e), 0.0, 0.0, 0.0], [0.0, np.sqrt(1 - p01e), 0.0, 0.0],
                   [0.0, 0.0, np.sqrt(1 - p10e), 0.0], [0.0, 0.0, 0.0, np.sqrt(1 - p11e)]])
    k00 = np.sqrt(p00e / 3) * (_flip_matrix(0, 1) + _flip_matrix(0, 2) + _flip_matrix(0, 3))
    k01 = np.sqrt(p01e / 3) * (_flip_matrix(1, 0) + _flip_matrix(1, 2) + _flip_matrix(1, 3))
    k10 = np.sqrt(p10e / 3) * (_flip_matrix(2, 0) + _flip_matrix(2, 1) + _flip_matrix(2, 3))
    k11 = np.sqrt(p11e / 3) * (_flip_matrix(3, 0) + _flip_matrix(3, 1) + _flip_matrix(3, 2))
    return kI, k00, k01, k10, k11


def _flip_matrix(i, j, dim=4):
    mat = np.zeros((dim, dim))
    # mat.itemset((i,j),1)
    mat.itemset((j, i), 1)
    return mat


def append_kraus_to_gate(kraus_ops, g):
    """
    Follow a gate `g` by a Kraus map described by `kraus_ops`.

    :param list kraus_ops: The Kraus operators.
    :param numpy.ndarray g: The unitary gate.
    :return: A list of transformed Kraus operators.
    """
    return [kj.dot(g) for kj in kraus_ops]

from pyquil.quil import DefGate

II_mat = np.eye(4)
II_definition = DefGate("II", II_mat)
II = II_definition.get_constructor()
kraus_ops = two_qubit_bit_flip_operators(0.7,1,1,1)

# will delete code above here
# ==================================================================================================


def symmetrization(program: Program, meas_qubits: List[int], symm_type: int = 3) \
        -> Tuple[List[Program], List[Tuple[bool]]]:
    """
    For the input program generate new programs which flip the measured qubits with an X gate in
    certain combinations in order to symmetrize readout.

    An expanded list of programs is returned along with a list of bools which indicates which
    qubits are flipped in each program.

    The symmetrization types are specified by an int; the types available are:
    -1 -- exhaustive symmetrization uses every possible combination of flips
     0 -- trivial that is no symmetrization
     1 -- symmetrization using an orthogonal array with strength 1
     2 -- symmetrization using an orthogonal array with strength 2
     3 -- symmetrization using an orthogonal array with strength 3
    By default a strength 3 orthogonal array (OA) is used; this ensures that expectations of the
    form <b_k b_j b_i> for bits any bits i,j,k will have symmetric readout errors. As a strength 3
    OA is also a strength 2 and 1 OA it also ensures <b_j b_i> and <b_i> for any bits j and i.

    :param programs: a program which will be symmetrized.
    :param meas_qubits: the groups of measurement qubits. Only these qubits will be symmetrized
        over, even if the program acts on other qubits.
    :param sym_type: an int determining the type of symmetrization performed.
    :return: a list of symmetrized programs, the corresponding array of bools indicating which
        qubits were flipped.
    """
    symm_programs = []
    flip_arrays = []

    if symm_type < -1 or symm_type > 3:
        raise ValueError("symm_type must be one of the following ints [-1, 0, 1, 2, 3].")
    elif symm_type == -1:
        # exhaustive = all possible binary strings
        flip_matrix = np.asarray(list(itertools.product([0, 1], repeat=len(meas_qubits))))
    elif symm_type >= 0:
        flip_matrix = construct_orthogonal_array(len(meas_qubits), symm_type)

    # The next part is not rigorous the sense that we simply truncate to the desired
    # number of qubits. The problem is that orthogonal arrays of a certain strength for an
    # arbitrary number of qubits are not known to exist.
    num_expts, num_qubits = flip_matrix.shape
    if len(meas_qubits) != num_qubits:
        flip_matrix = flip_matrix[0:int(num_expts), 0:int(len(meas_qubits))]

    for flip_array in flip_matrix:
        total_prog_symm = program.copy()
        prog_symm = _flip_array_to_prog(flip_array, meas_qubits)
        total_prog_symm += prog_symm
        symm_programs.append(total_prog_symm)
        flip_arrays.append(flip_array)

    # this hack is here only to test the symmetrization
    for prog in symm_programs:
        prog.inst(II_definition)
        prog.define_noisy_gate("II", [0, 1], append_kraus_to_gate(kraus_ops, II_mat))
        prog.inst(II(0, 1))

    return symm_programs, flip_arrays

def consolidate_symmetrization_outputs(outputs: List[np.ndarray], flip_arrays: List[Tuple[bool]],
                                       groups: List[int]) -> List[np.ndarray]:
    """
    Given bitarray results from a series of symmetrization programs, appropriately flip output
    bits and consolidate results into new bitarrays.

    :param outputs: a list of the raw bitarrays resulting from running a list of symmetrized
        programs; for example, the results returned from _measure_bitstrings
    :param flip_arrays: a list of boolean arrays in one-to-one correspondence with the list of
        outputs indicating which qubits where flipped before each bitarray was measured.
    :param groups: the group from which each symmetrized program was generated. E.g. if only one
        program was symmetrized then groups is simply [0] * len(outputs). The length of the
        returned consolidated outputs is exactly the number of distinct integers in groups.
    :return: a list of the consolidated bitarray outputs which can be treated as the symmetrized
        outputs of the original programs passed into a symmetrization method. See
        estimate_observables for example usage.
    """
    assert len(outputs) == len(groups) == len(flip_arrays)

    output = {group: [] for group in set(groups)}
    for bitarray, group, flip_array in zip(outputs, groups, flip_arrays):
        if len(flip_array) == 0:
            # happens when measuring identity.
            # TODO: better way of handling identity measurement? (in _measure_bitstrings too)
            output[group].append(bitarray)
        else:
            output[group].append(bitarray ^ flip_array)

    return [np.vstack(output[group]) for group in sorted(list(set(groups)))]

def _measure_bitstrings(qc, programs: List[Program], meas_qubits: List[List[int]],
                        num_shots = 600, use_compiler = False) -> List[np.ndarray]:
    """
    Wrapper for appending measure instructions onto each program, running the program,
    and accumulating the resulting bitarrays.

    By default each program is assumed to be native quil.

    :param qc: a quantum computer object on which to run each program
    :param programs: a list of programs to run
    :param meas_qubits: groups of qubits to measure for each program
    :param num_shots: the number of shots to run for each program
    :return: a len(programs) long list of num_shots by num_meas_qubits bit arrays of results for
        each program.
    """
    assert len(programs) == len(meas_qubits), 'The qubits to measure must be specified for each ' \
                                              'program, one list of qubits per program.'

    results = []
    for program, qubits in zip(programs, meas_qubits):
        if len(qubits) == 0:
            # corresponds to measuring identity; no program needs to be run.
            results.append(np.array([[]]))
            continue
        # copy the program so the original is not mutated
        prog = program.copy()
        ro = prog.declare('ro', 'BIT', len(qubits))
        for idx, q in enumerate(qubits):
            prog += MEASURE(q, ro[idx])

        prog.wrap_in_numshots_loop(num_shots)
        if use_compiler:
            prog = qc.compiler.quil_to_native_quil(prog)
        exe = qc.compiler.native_quil_to_executable(prog)
        shots = qc.run(exe)
        results.append(shots)
    return results


def construct_orthogonal_array(num_qubits: int, strength: int = 3):
    """
    Given a strength and number of qubits this function returns an Orthogonal Array (OA)
    on 'n' or more qubits. Sometimes the size of the returned array is larger than num_qubits;
    typically the next power of two relative to num_qubits. This is corrected later in the code
    flow.

    :param num_qubits: the minimum number of qubits the OA should act on.
    :param strength: the statistical "strength" of the OA
    :return: a numpy array where the rows represent the different experiments
    """
    if strength < 0 or strength > 3:
        raise ValueError("'strength' must be one of the following ints [0, 1, 2, 3].")
    if strength == 0:
        # trivial flip matrix = an array of zeros
        flip_matrix = np.zeros(num_qubits)
    elif symm_type == 1:
        # orthogonal array with strength equal to 1. See Example 1.4 of [OATA], referenced in the
        # `construct_strength_two_orthogonal_array` docstrings, for more details.
        flip_matrix = np.concatenate((np.zeros(num_qubits), np.ones(num_qubits)), axis=0)
    elif symm_type == 2:
        flip_matrix = construct_strength_two_orthogonal_array(num_qubits)
    elif symm_type == 3:
        flip_matrix = construct_strength_three_orthogonal_array(num_qubits)

    return flip_matrix

def _next_power_of_2(x):
    return 1 if x == 0 else 2 ** (x - 1).bit_length()


def construct_strength_three_orthogonal_array(num_qubits: int):
    r"""
    Given a number of qubits this function returns an Orthogonal Array (OA)
    on 'n' qubits where n is the next power of two relative to num_qubits.

    Specifically it returns an with the OA(2n, n, 2, 3), where

    OA(N, k, s, t)
    N: Number of rows, level combinations or runs
    k: Number of columns, constraints or factors
    s: Number of symbols or levels
    t: Strength

    See [OATA] for more details.

    [OATA] Orthogonal Arrays: theory and applications
           Hedayat, Sloane, Stufken
           Springer Science & Business Media, 2012.
           https://dx.doi.org/10.1007/978-1-4612-1478-6
    """
    num_qubits_power_of_2 = _next_power_of_2(num_qubits)
    H = hadamard(num_qubits_power_of_2)
    Hfold = np.concatenate((H, -H), axis=0)
    design = ((Hfold + 1) / 2).astype(int)
    return design


def construct_strength_two_orthogonal_array(num_qubits: int):
    r"""
    Given a number of qubits this function returns an Orthogonal Array (OA) on 'n-1' qubits
    where n-1 is the next integer lambda so that 4*lambda -1 is larger than num_qubits.

    Specifically it returns an with the OA(n, n − 1, 2, 2), where

    OA(N, k, s, t)
    N: Number of rows, level combinations or runs
    k: Number of columns, constraints or factors
    s: Number of symbols or levels
    t: Strength

    See [OATA] for more details.

    [OATA] Orthogonal Arrays: theory and applications
           Hedayat, Sloane, Stufken
           Springer Science & Business Media, 2012.
           https://dx.doi.org/10.1007/978-1-4612-1478-6
    """
    # next line will break post denali at 275 qubits
    # valid_num_qubits = 4 * lambda - 1
    valid_numbers = [4 * lam - 1 for lam in range(1, 70)]
    # 4 * lambda
    four_lam = int(min(x for x in valid_numbers if x >= num_qubits) + 1)
    H = hadamard(_next_power_of_2(four_lam))
    # The minus sign in front of H fixes the 0 <-> 1 inversion relative to the reference [OATA]
    design = ((-H[1:int(four_lam), 0:int(four_lam)] + 1) / 2).astype(int)
    return design.T

class QuantumComputer:
    def __init__(self, *,
                 name: str,
                 qam: QAM,
                 device: AbstractDevice,
                 compiler: AbstractCompiler,
                 symmetrize_readout: bool = False) -> None:
        """
        A quantum computer for running quantum programs.

        A quantum computer has various characteristics like supported gates, qubits, qubit
        topologies, gate fidelities, and more. A quantum computer also has the ability to
        run quantum programs.

        A quantum computer can be a real Rigetti QPU that uses superconducting transmon
        qubits to run quantum programs, or it can be an emulator like the Rigetti QVM with
        noise models and mimicked topologies.

        :param name: A string identifying this particular quantum computer.
        :param qam: A quantum abstract machine which handles executing quantum programs. This
            dispatches to a QVM or QPU.
        :param device: A collection of connected qubits and associated specs and topology.
        :param symmetrize_readout: Whether to apply readout error symmetrization. See
            :py:func:`run_symmetrized_readout` for a complete description.
        """
        self.name = name
        self.qam = qam
        self.device = device
        self.compiler = compiler

        self.symmetrize_readout = symmetrize_readout

    def qubits(self) -> List[int]:
        """
        Return a sorted list of this QuantumComputer's device's qubits

        See :py:func:`AbstractDevice.qubits` for more.
        """
        return self.device.qubits()

    def qubit_topology(self) -> nx.graph:
        """
        Return a NetworkX graph representation of this QuantumComputer's device's qubit
        connectivity.

        See :py:func:`AbstractDevice.qubit_topology` for more.
        """
        return self.device.qubit_topology()

    def get_isa(self, oneq_type: str = 'Xhalves',
                twoq_type: str = 'CZ') -> ISA:
        """
        Return a target ISA for this QuantumComputer's device.

        See :py:func:`AbstractDevice.get_isa` for more.

        :param oneq_type: The family of one-qubit gates to target
        :param twoq_type: The family of two-qubit gates to target
        """
        return self.device.get_isa(oneq_type=oneq_type, twoq_type=twoq_type)

    @_record_call
    def run(self, executable: Executable,
            memory_map: Dict[str, List[Union[int, float]]] = None) -> np.ndarray:
        """
        Run a quil executable. If the executable contains declared parameters, then a memory
        map must be provided, which defines the runtime values of these parameters.

        :param executable: The program to run. You are responsible for compiling this first.
        :param memory_map: The mapping of declared parameters to their values. The values
            are a list of floats or integers.
        :return: A numpy array of shape (trials, len(ro-register)) that contains 0s and 1s.
        """
        self.qam.load(executable)
        if memory_map:
            for region_name, values_list in memory_map.items():
                for offset, value in enumerate(values_list):
                    # TODO gh-658: have write_memory take a list rather than value + offset
                    self.qam.write_memory(region_name=region_name, offset=offset, value=value)
        return self.qam.run() \
            .wait() \
            .read_memory(region_name='ro')

    @_record_call
    def run_symmetrized_readout(self, program: Program, trials: int) -> np.ndarray:
        """
        Run a quil program in such a way that the readout error is made collectively symmetric

        This means the probability of a bitstring ``b`` being mistaken for a bitstring ``c`` is
        the same as the probability of ``not(b)`` being mistaken for ``not(c)``

        A more general symmetrization would guarantee that the probability of ``b`` being
        mistaken for ``c`` depends only on which bit of ``c`` are different from ``b``. This
        would require choosing random subsets of bits to flip.

        In a noisy device, the probability of accurately reading the 0 state might be higher
        than that of the 1 state. This makes correcting for readout more difficult. This
        function runs the program normally ``(trials//2)`` times. The other half of the time,
        it will insert an ``X`` gate prior to any ``MEASURE`` instruction and then flip the
        measured classical bit back.

        See :py:func:`run` for this function's parameter descriptions.
        """
        flipped_program = _get_flipped_protoquil_program(program)
        if trials % 2 != 0:
            raise ValueError("Using symmetrized measurement functionality requires that you "
                             "take an even number of trials.")
        half_trials = trials // 2
        flipped_program = flipped_program.wrap_in_numshots_loop(shots=half_trials)
        flipped_executable = self.compile(flipped_program)

        executable = self.compile(program.wrap_in_numshots_loop(half_trials))
        samples = self.run(executable)
        flipped_samples = self.run(flipped_executable)
        double_flipped_samples = np.logical_not(flipped_samples).astype(int)
        results = np.concatenate((samples, double_flipped_samples), axis=0)
        np.random.shuffle(results)
        return results

    @_record_call
    def run_symmetrized_readout_new(self, program: Program, trials: int,  symm_type: str = 'thr',
                                    meas_qubits: List[int] = None, use_compiler: bool = True)\
            -> np.ndarray:
        """
        Run a quil program in such a way that the readout error is made collectively symmetric

        This means the probability of a bitstring ``b`` being mistaken for a bitstring ``c`` is
        the same as the probability of ``not(b)`` being mistaken for ``not(c)``

        A more general symmetrization would guarantee that the probability of ``b`` being
        mistaken for ``c`` depends only on which bit of ``c`` are different from ``b``. This
        would require choosing random subsets of bits to flip.

        In a noisy device, the probability of accurately reading the 0 state might be higher
        than that of the 1 state. This makes correcting for readout more difficult. This
        function runs the program normally ``(trials//2)`` times. The other half of the time,
        it will insert an ``X`` gate prior to any ``MEASURE`` instruction and then flip the
        measured classical bit back.

        See :py:func:`run` for this function's parameter descriptions.
        """
        if len(symm_type) is not 3:
            raise ValueError("Symmetrization options are indicated by a length three string. See "
                             "the docstrings for more information.")

        if trials % 2 != 0:
            raise ValueError("Using symmetrized measurement functionality requires that you "
                             "take an even number of trials.")

        if meas_qubits is None:
            meas_qubits = [list(program.get_qubits())]

        max_num_qubits= max( [len(qs) for qs in meas_qubits])

        if trials <= max_num_qubits:
            trials = _next_power_of_2(2*max_num_qubits)
            warnings.warn('Number of trials was too low it is now '+str(trials))

        sym_programs, sym_meas_qs, flip_arrays, prog_groups = symmetrization([program],
                                                                             meas_qubits,
                                                                             symm_type)
        # This will be 1 or a power of two
        num_sym_progs = len(sym_programs)

        num_shots_per_prog =  trials // num_sym_progs
        results = _measure_bitstrings(self,
                                      sym_programs,
                                      sym_meas_qs,
                                      num_shots_per_prog,
                                      use_compiler)

        conso_results = consolidate_symmetrization_outputs(results, flip_arrays, prog_groups)

        return conso_results[0]

    @_record_call
    def run_and_measure(self, program: Program, trials: int) -> Dict[int, np.ndarray]:
        """
        Run the provided state preparation program and measure all qubits.

        This will measure all the qubits on this QuantumComputer, not just qubits
        that are used in the program.

        The returned data is a dictionary keyed by qubit index because qubits for a given
        QuantumComputer may be non-contiguous and non-zero-indexed. To turn this dictionary
        into a 2d numpy array of bitstrings, consider::

            bitstrings = qc.run_and_measure(...)
            bitstring_array = np.vstack(bitstrings[q] for q in qc.qubits()).T
            bitstring_array.shape  # (trials, len(qc.qubits()))

        .. note::

            In contrast to :py:class:`QVMConnection.run_and_measure`, this method simulates
            noise correctly for noisy QVMs. However, this method is slower for ``trials > 1``.
            For faster noise-free simulation, consider
            :py:class:`WavefunctionSimulator.run_and_measure`.

        :param program: The state preparation program to run and then measure.
        :param trials: The number of times to run the program.
        :return: A dictionary keyed by qubit index where the corresponding value is a 1D array of
            measured bits.
        """
        program = program.copy()
        validate_supported_quil(program)
        ro = program.declare('ro', 'BIT', len(self.qubits()))
        for i, q in enumerate(self.qubits()):
            program.inst(MEASURE(q, ro[i]))
        program.wrap_in_numshots_loop(trials)
        executable = self.compile(program)
        bitstring_array = self.run(executable=executable)
        bitstring_dict = {}
        for i, q in enumerate(self.qubits()):
            bitstring_dict[q] = bitstring_array[:, i]
        return bitstring_dict

    @_record_call
    def compile(self, program: Program,
                to_native_gates: bool = True,
                optimize: bool = True) -> Union[BinaryExecutableResponse, PyQuilExecutableResponse]:
        """
        A high-level interface to program compilation.

        Compilation currently consists of two stages. Please see the :py:class:`AbstractCompiler`
        docs for more information. This function does all stages of compilation.

        Right now both ``to_native_gates`` and ``optimize`` must be either both set or both
        unset. More modular compilation passes may be available in the future.

        :param program: A Program
        :param to_native_gates: Whether to compile non-native gates to native gates.
        :param optimize: Whether to optimize programs to reduce the number of operations.
        :return: An executable binary suitable for passing to :py:func:`QuantumComputer.run`.
        """
        flags = [to_native_gates, optimize]
        assert all(flags) or all(not f for f in flags), "Must turn quilc all on or all off"
        quilc = all(flags)

        if quilc:
            nq_program = self.compiler.quil_to_native_quil(program)
        else:
            nq_program = program
        binary = self.compiler.native_quil_to_executable(nq_program)
        return binary

    def reset(self):
        """
        Reset the QuantumComputer's QAM to its initial state.
        """
        self.qam.reset()

    def __str__(self) -> str:
        return self.name

    def __repr__(self):
        return f'QuantumComputer[name="{self.name}"]'


@_record_call
def list_quantum_computers(connection: ForestConnection = None,
                           qpus: bool = True,
                           qvms: bool = True) -> List[str]:
    """
    List the names of available quantum computers

    :param connection: An optional :py:class:ForestConnection` object. If not specified,
        the default values for URL endpoints will be used, and your API key
        will be read from ~/.pyquil_config. If you deign to change any
        of these parameters, pass your own :py:class:`ForestConnection` object.
    :param qpus: Whether to include QPU's in the list.
    :param qvms: Whether to include QVM's in the list.
    """
    if connection is None:
        connection = ForestConnection()

    qc_names: List[str] = []
    if qpus:
        qc_names += list(list_lattices(connection=connection).keys())

    if qvms:
        qc_names += ['9q-square-qvm', '9q-square-noisy-qvm']

    return qc_names


def _parse_name(name: str, as_qvm: bool, noisy: bool) -> Tuple[str, str, bool]:
    """
    Try to figure out whether we're getting a (noisy) qvm, and the associated qpu name.

    See :py:func:`get_qc` for examples of valid names + flags.
    """
    parts = name.split('-')
    if len(parts) >= 2 and parts[-2] == 'noisy' and parts[-1] in ['qvm', 'pyqvm']:
        if as_qvm is not None and (not as_qvm):
            raise ValueError("The provided qc name indicates you are getting a noisy QVM, "
                             "but you have specified `as_qvm=False`")

        if noisy is not None and (not noisy):
            raise ValueError("The provided qc name indicates you are getting a noisy QVM, "
                             "but you have specified `noisy=False`")

        qvm_type = parts[-1]
        noisy = True
        prefix = '-'.join(parts[:-2])
        return prefix, qvm_type, noisy

    if len(parts) >= 1 and parts[-1] in ['qvm', 'pyqvm']:
        if as_qvm is not None and (not as_qvm):
            raise ValueError("The provided qc name indicates you are getting a QVM, "
                             "but you have specified `as_qvm=False`")
        qvm_type = parts[-1]
        if noisy is None:
            noisy = False
        prefix = '-'.join(parts[:-1])
        return prefix, qvm_type, noisy

    if as_qvm is not None and as_qvm:
        qvm_type = 'qvm'
    else:
        qvm_type = None

    if noisy is None:
        noisy = False

    return name, qvm_type, noisy


def _canonicalize_name(prefix, qvm_type, noisy):
    """Take the output of _parse_name to create a canonical name.
    """
    if noisy:
        noise_suffix = '-noisy'
    else:
        noise_suffix = ''

    if qvm_type is None:
        qvm_suffix = ''
    elif qvm_type == 'qvm':
        qvm_suffix = '-qvm'
    elif qvm_type == 'pyqvm':
        qvm_suffix = '-pyqvm'
    else:
        raise ValueError(f"Unknown qvm_type {qvm_type}")

    name = f'{prefix}{noise_suffix}{qvm_suffix}'
    return name


def _get_qvm_or_pyqvm(qvm_type, connection, noise_model=None, device=None,
                      requires_executable=False):
    if qvm_type == 'qvm':
        return QVM(connection=connection, noise_model=noise_model,
                   requires_executable=requires_executable)
    elif qvm_type == 'pyqvm':
        return PyQVM(n_qubits=device.qubit_topology().number_of_nodes())

    raise ValueError("Unknown qvm type {}".format(qvm_type))


def _get_qvm_qc(name: str, qvm_type: str, device: AbstractDevice, noise_model: NoiseModel = None,
                requires_executable: bool = False,
                connection: ForestConnection = None) -> QuantumComputer:
    """Construct a QuantumComputer backed by a QVM.

    This is a minimal wrapper over the QuantumComputer, QVM, and QVMCompiler constructors.

    :param name: A string identifying this particular quantum computer.
    :param qvm_type: The type of QVM. Either qvm or pyqvm.
    :param device: A device following the AbstractDevice interface.
    :param noise_model: An optional noise model
    :param requires_executable: Whether this QVM will refuse to run a :py:class:`Program` and
        only accept the result of :py:func:`compiler.native_quil_to_executable`. Setting this
        to True better emulates the behavior of a QPU.
    :param connection: An optional :py:class:`ForestConnection` object. If not specified,
        the default values for URL endpoints will be used.
    :return: A QuantumComputer backed by a QVM with the above options.
    """
    if connection is None:
        connection = ForestConnection()

    return QuantumComputer(name=name,
                           qam=_get_qvm_or_pyqvm(
                               qvm_type=qvm_type,
                               connection=connection,
                               noise_model=noise_model,
                               device=device,
                               requires_executable=requires_executable),
                           device=device,
                           compiler=QVMCompiler(
                               device=device,
                               endpoint=connection.compiler_endpoint))


def _get_qvm_with_topology(name: str, topology: nx.Graph,
                           noisy: bool = False,
                           requires_executable: bool = True,
                           connection: ForestConnection = None,
                           qvm_type: str = 'qvm') -> QuantumComputer:
    """Construct a QVM with the provided topology.

    :param name: A name for your quantum computer. This field does not affect behavior of the
        constructed QuantumComputer.
    :param topology: A graph representing the desired qubit connectivity.
    :param noisy: Whether to include a generic noise model. If you want more control over
        the noise model, please construct your own :py:class:`NoiseModel` and use
        :py:func:`_get_qvm_qc` instead of this function.
    :param requires_executable: Whether this QVM will refuse to run a :py:class:`Program` and
        only accept the result of :py:func:`compiler.native_quil_to_executable`. Setting this
        to True better emulates the behavior of a QPU.
    :param connection: An optional :py:class:`ForestConnection` object. If not specified,
        the default values for URL endpoints will be used.
    :param qvm_type: The type of QVM. Either 'qvm' or 'pyqvm'.
    :return: A pre-configured QuantumComputer
    """
    # Note to developers: consider making this function public and advertising it.
    device = NxDevice(topology=topology)
    if noisy:
        noise_model = decoherence_noise_with_asymmetric_ro(gates=gates_in_isa(device.get_isa()))
    else:
        noise_model = None
    return _get_qvm_qc(name=name, qvm_type=qvm_type, connection=connection, device=device,
                       noise_model=noise_model, requires_executable=requires_executable)


def _get_9q_square_qvm(name: str, noisy: bool,
                       connection: ForestConnection = None,
                       qvm_type: str = 'qvm') -> QuantumComputer:
    """
    A nine-qubit 3x3 square lattice.

    This uses a "generic" lattice not tied to any specific device. 9 qubits is large enough
    to do vaguely interesting algorithms and small enough to simulate quickly.

    :param name: The name of this QVM
    :param connection: The connection to use to talk to external services
    :param noisy: Whether to construct a noisy quantum computer
    :param qvm_type: The type of QVM. Either 'qvm' or 'pyqvm'.
    :return: A pre-configured QuantumComputer
    """
    topology = nx.convert_node_labels_to_integers(nx.grid_2d_graph(3, 3))
    return _get_qvm_with_topology(name=name, connection=connection,
                                  topology=topology,
                                  noisy=noisy,
                                  requires_executable=True,
                                  qvm_type=qvm_type)


def _get_unrestricted_qvm(name: str, noisy: bool,
                          n_qubits: int = 34,
                          connection: ForestConnection = None,
                          qvm_type: str = 'qvm') -> QuantumComputer:
    """
    A qvm with a fully-connected topology.

    This is obviously the least realistic QVM, but who am I to tell users what they want.

    :param name: The name of this QVM
    :param noisy: Whether to construct a noisy quantum computer
    :param n_qubits: 34 qubits ought to be enough for anybody.
    :param connection: The connection to use to talk to external services
    :param qvm_type: The type of QVM. Either 'qvm' or 'pyqvm'.
    :return: A pre-configured QuantumComputer
    """
    topology = nx.complete_graph(n_qubits)
    return _get_qvm_with_topology(name=name, connection=connection,
                                  topology=topology,
                                  noisy=noisy,
                                  requires_executable=False,
                                  qvm_type=qvm_type)


def _get_qvm_based_on_real_device(name: str, device: Device,
                                  noisy: bool, connection: ForestConnection = None,
                                  qvm_type: str = 'qvm'):
    """
    A qvm with a based on a real device.

    This is the most realistic QVM.

    :param name: The full name of this QVM
    :param device: The device from :py:func:`get_lattice`.
    :param noisy: Whether to construct a noisy quantum computer by using the device's
        associated noise model.
    :param connection: An optional :py:class:`ForestConnection` object. If not specified,
        the default values for URL endpoints will be used.
    :return: A pre-configured QuantumComputer based on the named device.
    """
    if noisy:
        noise_model = device.noise_model
    else:
        noise_model = None
    return _get_qvm_qc(name=name, connection=connection, device=device,
                       noise_model=noise_model, requires_executable=True,
                       qvm_type=qvm_type)


@_record_call
def get_qc(name: str, *, as_qvm: bool = None, noisy: bool = None,
           connection: ForestConnection = None) -> QuantumComputer:
    """
    Get a quantum computer.

    A quantum computer is an object of type :py:class:`QuantumComputer` and can be backed
    either by a QVM simulator ("Quantum/Quil Virtual Machine") or a physical Rigetti QPU ("Quantum
    Processing Unit") made of superconducting qubits.

    You can choose the quantum computer to target through a combination of its name and optional
    flags. There are multiple ways to get the same quantum computer. The following are equivalent::

        >>> qc = get_qc("Aspen-1-16Q-A-noisy-qvm")
        >>> qc = get_qc("Aspen-1-16Q-A", as_qvm=True, noisy=True)

    and will construct a simulator of an Aspen-1 lattice with a noise model based on device
    characteristics. We also provide a means for constructing generic quantum simulators that
    are not related to a given piece of Rigetti hardware::

        >>> qc = get_qc("9q-square-qvm")
        >>> qc = get_qc("9q-square", as_qvm=True)

    Finally, you can get request a QVM with "no" topology of a given number of qubits
    (technically, it's a fully connected graph among the given number of qubits) with::

        >>> qc = get_qc("5q-qvm") # or "6q-qvm", or "34q-qvm", ...

    These less-realistic, fully-connected QVMs will also be more lenient on what types of programs
    they will ``run``. Specifically, you do not need to do any compilation. For the other, realistic
    QVMs you must use :py:func:`qc.compile` or :py:func:`qc.compiler.native_quil_to_executable`
    prior to :py:func:`qc.run`.

    The Rigetti QVM must be downloaded from https://www.rigetti.com/forest and run as a server
    alongside your python program. To use pyQuil's built-in QVM, replace all ``"-qvm"`` suffixes
    with ``"-pyqvm"``::

        >>> qc = get_qc("5q-pyqvm")

    Redundant flags are acceptable, but conflicting flags will raise an exception::

        >>> qc = get_qc("9q-square-qvm") # qc is fully specified by its name
        >>> qc = get_qc("9q-square-qvm", as_qvm=True) # redundant, but ok
        >>> qc = get_qc("9q-square-qvm", as_qvm=False) # Error!

    Use :py:func:`list_quantum_computers` to retrieve a list of known qc names.

    This method is provided as a convenience to quickly construct and use QVM's and QPU's.
    Power users may wish to have more control over the specification of a quantum computer
    (e.g. custom noise models, bespoke topologies, etc.). This is possible by constructing
    a :py:class:`QuantumComputer` object by hand. Please refer to the documentation on
    :py:class:`QuantumComputer` for more information.

    :param name: The name of the desired quantum computer. This should correspond to a name
        returned by :py:func:`list_quantum_computers`. Names ending in "-qvm" will return
        a QVM. Names ending in "-pyqvm" will return a :py:class:`PyQVM`. Names ending in
        "-noisy-qvm" will return a QVM with a noise model. Otherwise, we will return a QPU with
        the given name.
    :param as_qvm: An optional flag to force construction of a QVM (instead of a QPU). If
        specified and set to ``True``, a QVM-backed quantum computer will be returned regardless
        of the name's suffix
    :param noisy: An optional flag to force inclusion of a noise model. If
        specified and set to ``True``, a quantum computer with a noise model will be returned
        regardless of the name's suffix. The noise model for QVMs based on a real QPU
        is an empirically parameterized model based on real device noise characteristics.
        The generic QVM noise model is simple T1 and T2 noise plus readout error. See
        :py:func:`~pyquil.noise.decoherence_noise_with_asymmetric_ro`.
    :param connection: An optional :py:class:`ForestConnection` object. If not specified,
        the default values for URL endpoints will be used. If you deign to change any
        of these parameters, pass your own :py:class:`ForestConnection` object.
    :return: A pre-configured QuantumComputer
    """
    # 1. Parse name, check for redundant options, canonicalize names.
    prefix, qvm_type, noisy = _parse_name(name, as_qvm, noisy)
    del as_qvm  # do not use after _parse_name
    name = _canonicalize_name(prefix, qvm_type, noisy)

    # 2. Check for unrestricted {n}q-qvm
    ma = re.fullmatch(r'(\d+)q', prefix)
    if ma is not None:
        n_qubits = int(ma.group(1))
        if qvm_type is None:
            raise ValueError("Please name a valid device or run as a QVM")
        return _get_unrestricted_qvm(name=name, connection=connection,
                                     noisy=noisy, n_qubits=n_qubits, qvm_type=qvm_type)

    # 3. Check for "9q-square" qvm
    if prefix == '9q-generic' or prefix == '9q-square':
        if prefix == '9q-generic':
            warnings.warn("Please prefer '9q-square' instead of '9q-generic'", DeprecationWarning)

        if qvm_type is None:
            raise ValueError("The device '9q-square' is only available as a QVM")
        return _get_9q_square_qvm(name=name, connection=connection, noisy=noisy, qvm_type=qvm_type)

    # 4. Not a special case, query the web for information about this device.
    device = get_lattice(prefix)
    if qvm_type is not None:
        # 4.1 QVM based on a real device.
        return _get_qvm_based_on_real_device(name=name, device=device,
                                             noisy=noisy, connection=connection, qvm_type=qvm_type)
    else:
        # 4.2 A real device
        if noisy is not None and noisy:
            warnings.warn("You have specified `noisy=True`, but you're getting a QPU. This flag "
                          "is meant for controlling noise models on QVMs.")
        return QuantumComputer(name=name,
                               qam=QPU(
                                   endpoint=pyquil_config.qpu_url,
                                   user=pyquil_config.user_id),
                               device=device,
                               compiler=QPUCompiler(
                                   quilc_endpoint=pyquil_config.quilc_url,
                                   qpu_compiler_endpoint=pyquil_config.qpu_compiler_url,
                                   device=device,
                                   name=prefix))


@contextmanager
def local_qvm() -> Iterator[Tuple[subprocess.Popen, subprocess.Popen]]:
    """A context manager for the Rigetti local QVM and QUIL compiler.

    You must first have installed the `qvm` and `quilc` executables from
    the forest SDK. [https://www.rigetti.com/forest]

    This context manager will start up external processes for both the
    compiler and virtual machine, and then terminate them when the context
    is exited.

    If `qvm` (or `quilc`) is already running, then the existing process will
    be used, and will not terminated at exit.

    >>> from pyquil import get_qc, Program
    >>> from pyquil.gates import CNOT, Z
    >>> from pyquil.api import local_qvm
    >>>
    >>> qvm = get_qc('9q-square-qvm')
    >>> prog = Program(Z(0), CNOT(0, 1))
    >>>
    >>> with local_qvm():
    >>>     results = qvm.run_and_measure(prog, trials=10)

    :raises: FileNotFoundError: If either executable is not installed.
    """
    # Enter. Acquire resource
    qvm = subprocess.Popen(['qvm', '-S'],
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)

    quilc = subprocess.Popen(['quilc', '-RP'],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)

    # Return context
    try:
        yield (qvm, quilc)

    finally:
        # Exit. Release resource
        qvm.terminate()
        quilc.terminate()
