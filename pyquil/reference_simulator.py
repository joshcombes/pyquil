import warnings

import numpy as np
from numpy.random.mtrand import RandomState
from typing import Union, List, Sequence

from pyquil.gate_matrices import P0, P1, KRAUS_OPS, QUANTUM_GATES
from pyquil.paulis import PauliTerm, PauliSum
from pyquil.pyqvm import AbstractQuantumSimulator
from pyquil.quilbase import Gate
from pyquil.unitary_tools import lifted_gate_matrix, lifted_gate, all_bitstrings


def _term_expectation(wf, term: PauliTerm, n_qubits):
    # Computes <psi|XYZ..XXZ|psi>
    wf2 = wf
    for qubit_i, op_str in term._ops.items():
        # Re-use QUANTUM_GATES since it has X, Y, Z
        op_mat = QUANTUM_GATES[op_str]
        op_mat = lifted_gate_matrix(matrix=op_mat, qubit_inds=[qubit_i], n_qubits=n_qubits)
        wf2 = op_mat @ wf2

    # `wf2` is XYZ..XXZ|psi>
    # hit it with a <psi| i.e. `wf.dag`
    return term.coefficient * (wf.conj().T @ wf2)


def _is_valid_quantum_state(state_matrix: np.ndarray, rtol=1e-05, atol=1e-08) -> bool:
    """
    Checks if a quantum state is valid, i.e. the matrix is Hermitian; trace one, and that the
    eigenvalues are non-negative.

    :param state_matrix: a D by D np.ndarray representing a quantum state
    :param rtol: The relative tolerance parameter in np.allclose and np.isclose
    :param atol: The absolute tolerance parameter in np.allclose and np.isclose
    :return: bool
    """
    hermitian = np.allclose(state_matrix, np.conjugate(state_matrix.transpose()), rtol, atol)
    if not hermitian:
        raise ValueError("The state matrix is not Hermitian.")
    trace_one = np.isclose(np.trace(state_matrix), 1, rtol, atol)
    if not trace_one:
        raise ValueError("The state matrix is not trace one.")
    evals = np.linalg.eigvals(state_matrix)
    non_neg_eigs = all([False if val < -atol else True for val in evals])
    if not non_neg_eigs:
        raise ValueError("The state matrix has negative Eigenvalues of the order -"+str(atol)+".")
    return hermitian and trace_one and non_neg_eigs


class ReferenceWavefunctionSimulator(AbstractQuantumSimulator):
    def __init__(self, n_qubits: int, rs: RandomState = None):
        """
        A wavefunction simulator that prioritizes readability over performance.

        Please consider using
        :py:class:`PyQVM(..., wf_simulator_type=ReferenceWavefunctionSimulator)` rather
        than using this class directly.

        This class uses a flat state-vector of length 2^n_qubits to store wavefunction
        amplitudes. The basis is taken to be bitstrings ordered lexicographically with
        qubit 0 as the rightmost bit. This is the same as the Rigetti Lisp QVM.

        :param n_qubits: Number of qubits to simulate.
        :param rs: a RandomState (should be shared with the owning :py:class:`PyQVM`) for
            doing anything stochastic. A value of ``None`` disallows doing anything stochastic.
        """
        self.n_qubits = n_qubits
        self.rs = rs

        self.wf = np.zeros(2 ** n_qubits, dtype=np.complex128)
        self.wf[0] = complex(1.0, 0)

    def sample_bitstrings(self, n_samples):
        """
        Sample bitstrings from the distribution defined by the wavefunction.

        Qubit 0 is at ``out[:, 0]``.

        :param n_samples: The number of bitstrings to sample
        :return: An array of shape (n_samples, n_qubits)
        """
        if self.rs is None:
            raise ValueError("You have tried to perform a stochastic operation without setting the "
                             "random state of the simulator. Might I suggest using a PyQVM object?")
        probabilities = np.abs(self.wf) ** 2
        possible_bitstrings = all_bitstrings(self.n_qubits)
        inds = self.rs.choice(2 ** self.n_qubits, n_samples, p=probabilities)
        bitstrings = possible_bitstrings[inds, :]
        bitstrings = np.flip(bitstrings, axis=1)  # qubit ordering: 0 on the left.
        return bitstrings

    def do_gate(self, gate: Gate):
        """
        Perform a gate.

        :return: ``self`` to support method chaining.
        """
        unitary = lifted_gate(gate=gate, n_qubits=self.n_qubits)
        self.wf = unitary.dot(self.wf)
        return self

    def do_gate_matrix(self, matrix: np.ndarray, qubits: Sequence[int]):
        """
        Apply an arbitrary unitary; not necessarily a named gate.

        :param matrix: The unitary matrix to apply. No checks are done.
        :param qubits: The qubits to apply the unitary to.
        :return: ``self`` to support method chaining.
        """
        unitary = lifted_gate_matrix(matrix, list(qubits), n_qubits=self.n_qubits)
        self.wf = unitary.dot(self.wf)
        return self

    def do_measurement(self, qubit: int) -> int:
        """
        Measure a qubit, collapse the wavefunction, and return the measurement result.

        :param qubit: Index of the qubit to measure.
        :return: measured bit
        """
        if self.rs is None:
            raise ValueError("You have tried to perform a stochastic operation without setting the "
                             "random state of the simulator. Might I suggest using a PyQVM object?")
        # lift projective measure operator to Hilbert space
        # prob(0) = <psi P0 | P0 psi> = psi* . P0* . P0 . psi
        measure_0 = lifted_gate_matrix(matrix=P0, qubit_inds=[qubit], n_qubits=self.n_qubits)
        proj_psi = measure_0 @ self.wf
        prob_zero = np.conj(proj_psi).T @ proj_psi

        # generate random number to 'roll' for measure
        if self.rs.uniform() < prob_zero:
            # decohere state using the measure_0 operator
            unitary = measure_0 @ (np.eye(2 ** self.n_qubits) / np.sqrt(prob_zero))
            self.wf = unitary.dot(self.wf)
            return 0
        else:  # measure one
            measure_1 = lifted_gate_matrix(matrix=P1, qubit_inds=[qubit], n_qubits=self.n_qubits)
            unitary = measure_1 @ (np.eye(2 ** self.n_qubits) / np.sqrt(1 - prob_zero))
            self.wf = unitary.dot(self.wf)
            return 1

    def expectation(self, operator: Union[PauliTerm, PauliSum]):
        """
        Compute the expectation of an operator.

        :param operator: The operator
        :return: The operator's expectation value
        """
        if not isinstance(operator, PauliSum):
            operator = PauliSum([operator])

        return sum(_term_expectation(self.wf, term, n_qubits=self.n_qubits) for term in operator)

    def reset(self):
        """
        Reset the wavefunction to the |000...00> state.

        :return: ``self`` to support method chaining.
        """
        self.wf.fill(0)
        self.wf[0] = complex(1.0, 0)
        return self

    def do_post_gate_noise(self, noise_type: str, noise_prob: float,
                           qubits: List[int]) -> 'AbstractQuantumSimulator':
        raise NotImplementedError("The reference wavefunction simulator cannot handle noise")


class ReferenceDensitySimulator(AbstractQuantumSimulator):
    """
    A density matrix simulator that prioritizes readability over performance.

    Please consider using
    :py:class:`PyQVM(..., wf_simulator_type=ReferenceDensitySimulator)` rather
    than using this class directly.

    This class uses a dense matrix of shape ``(2^n_qubits, 2^n_qubits)`` to store the
    density matrix.

    :param n_qubits: Number of qubits to simulate.
    :param rs: a RandomState (should be shared with the owning :py:class:`PyQVM`) for
        doing anything stochastic. A value of ``None`` disallows doing anything stochastic.
    """

    def __init__(self, n_qubits: int, rs: RandomState = None):
        self.n_qubits = n_qubits
        self.rs = rs
        self.initial_density = np.zeros((2 ** n_qubits, 2 ** n_qubits), dtype=np.complex128)
        self.initial_density[0, 0] = complex(1.0, 0)
        self.density = self.initial_density

    def set_initial_state(self, state_matrix):
        """
        This method is the correct way (TM) to update the initial state matrix that is
        initialized every time reset() is called. The default initial state of
        ReferenceDensitySimulator is |000...00>.

        Note that the current state matrix, i.e. ``self.density`` is not affected by this
        method; you must change it directly or else call reset() after calling this method.

        To restore default state initialization behavior of ReferenceDensitySimulator pass in
        ``state_matrix = None`` and then call reset().

        :param state_matrix: numpy.ndarray or None.
        :return: ``self`` to support method chaining.
        """
        if state_matrix is None:
            self.initial_density = np.zeros((2 ** self.n_qubits, 2 ** self.n_qubits),
                                            dtype=np.complex128)
            self.initial_density[0, 0] = complex(1.0, 0)
        else:
            rows, cols = state_matrix.shape
            if rows != cols:
                raise ValueError("The state matrix is not square.")
            if self.n_qubits != int(np.log2(rows)):
                raise ValueError("The state matrix is not defined on the same numbers of qubits as "
                                 "the QVM.")
            if _is_valid_quantum_state(state_matrix):
                self.initial_density = state_matrix
            else:
                raise ValueError("The state matrix is not valid. It must be Hermitian, trace one, "
                                 "and have non-negative eigenvalues.")
        return self

    def sample_bitstrings(self, n_samples, tol_factor: float = 1e8):
        """
        Sample bitstrings from the distribution defined by the wavefunction.

        Qubit 0 is at ``out[:, 0]``.

        :param n_samples: The number of bitstrings to sample
        :param tol_factor: Tolerance to set imaginary probabilities to zero, relative to
            machine epsilon.
        :return: An array of shape (n_samples, n_qubits)
        """
        if self.rs is None:
            raise ValueError("You have tried to perform a stochastic operation without setting the "
                             "random state of the simulator. Might I suggest using a PyQVM object?")

        # for np.real_if_close the actual tolerance is (machine_eps * tol_factor),
        # where `machine_epsilon = np.finfo(float).eps`. If we use tol_factor = 1e8, then the
        # overall tolerance is \approx 2.2e-8.
        probabilities = np.real_if_close(np.diagonal(self.density), tol=tol_factor)
        # Next set negative probabilities to zero
        probabilities = [0 if p < 0.0 else p for p in probabilities]
        # Ensure they sum to one
        probabilities = probabilities / np.sum(probabilities)
        possible_bitstrings = all_bitstrings(self.n_qubits)
        inds = self.rs.choice(2 ** self.n_qubits, n_samples, p=probabilities)
        bitstrings = possible_bitstrings[inds, :]
        bitstrings = np.flip(bitstrings, axis=1)  # qubit ordering: 0 on the left.
        return bitstrings

    def do_gate(self, gate: Gate) -> 'AbstractQuantumSimulator':
        """
        Perform a gate.

        :return: ``self`` to support method chaining.
        """
        unitary = lifted_gate(gate=gate, n_qubits=self.n_qubits)
        self.density = unitary.dot(self.density).dot(np.conj(unitary).T)
        return self

    def do_gate_matrix(self, matrix: np.ndarray,
                       qubits: Sequence[int]) -> 'AbstractQuantumSimulator':
        """
        Apply an arbitrary unitary; not necessarily a named gate.

        :param matrix: The unitary matrix to apply. No checks are done
        :param qubits: A list of qubits to apply the unitary to.
        :return: ``self`` to support method chaining.
        """
        unitary = lifted_gate_matrix(matrix=matrix, qubit_inds=qubits, n_qubits=self.n_qubits)
        self.density = unitary.dot(self.density).dot(np.conj(unitary).T)
        return self

    def do_measurement(self, qubit: int) -> int:
        """
        Measure a qubit and collapse the wavefunction

        :return: The measurement result. A 1 or a 0.
        """
        if self.rs is None:
            raise ValueError("You have tried to perform a stochastic operation without setting the "
                             "random state of the simulator. Might I suggest using a PyQVM object?")
        measure_0 = lifted_gate_matrix(matrix=P0, qubit_inds=[qubit], n_qubits=self.n_qubits)
        prob_zero = np.trace(measure_0 @ self.density)

        # generate random number to 'roll' for measurement
        if self.rs.uniform() < prob_zero:
            # decohere state using the measure_0 operator
            unitary = measure_0 @ (np.eye(2 ** self.n_qubits) / np.sqrt(prob_zero))
            self.density = unitary.dot(self.density).dot(np.conj(unitary.T))
            return 0
        else:  # measure one
            measure_1 = lifted_gate_matrix(matrix=P1, qubit_inds=[qubit], n_qubits=self.n_qubits)
            unitary = measure_1 @ (np.eye(2 ** self.n_qubits) / np.sqrt(1 - prob_zero))
            self.density = unitary.dot(self.density).dot(np.conj(unitary.T))
            return 1

    def expectation(self, operator: Union[PauliTerm, PauliSum]):
        raise NotImplementedError("To implement")

    def reset(self) -> 'AbstractQuantumSimulator':
        """
        Resets the current state of ReferenceDensitySimulator ``self.density`` to
        ``self.initial_density``.

        :return: ``self`` to support method chaining.
        """
        self.density = self.initial_density
        return self

    def do_post_gate_noise(self, noise_type: str, noise_prob: float, qubits: List[int]):
        kraus_ops = KRAUS_OPS[noise_type](p=noise_prob)
        if np.isclose(noise_prob, 0.0):
            warnings.warn(f"Skipping {noise_type} post-gate noise because noise_prob is close to 0")
            return self

        for q in qubits:
            new_density = np.zeros_like(self.density)
            for kraus_op in kraus_ops:
                lifted_kraus_op = lifted_gate_matrix(matrix=kraus_op, qubit_inds=[q],
                                                     n_qubits=self.n_qubits)
                new_density += lifted_kraus_op.dot(self.density).dot(np.conj(lifted_kraus_op.T))
            self.density = new_density
        return self
