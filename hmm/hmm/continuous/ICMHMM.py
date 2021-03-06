__author__ = 'diego'

from hmm._BaseHMM import _BaseHMM
from hmm.kernels.icm import icm
from scipy import optimize
from scipy import stats
import multiprocessing as mp
import numpy as np
import GPy


class LFMHMMError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)


def _parallel_hyperparameter_objective(tup):
    '''
    Auxiliar function to make it easier to evaluate the objective function for
    emission parameters optimization in a parallel way.
    '''
    idx, d = tup
    gamma = d['gammas'][idx]
    mvgs = d['mvgs']
    observation = d['obs'][idx]
    n = len(mvgs)
    n_observations = len(gamma)
    weighted_sum = 0.0
    for i in range(n):
        mvg = mvgs[i]
        for t in range(n_observations):
            weighted_sum += gamma[t][i] * mvg.logpdf(observation[t])
    return weighted_sum

class ICMHMM(_BaseHMM):

    def __init__(self, number_outputs, n, locations_per_segment, start_t, end_t,
                 precision=np.double, verbose=False):
        assert n > 0
        assert locations_per_segment > 0
        assert number_outputs > 0
        assert type(number_outputs) is type(1)
        self.n = n  # number of hidden states
        self.number_outputs = number_outputs
        self.start_t = start_t
        self.end_t = end_t
        self.sample_locations = np.linspace(start_t, end_t,
                                            locations_per_segment)
        self.locations_per_segment = locations_per_segment
        # Pool of workers to perform parallel computations when need it.
        self.pool = mp.Pool()
        # covariance memoization
        self.memo_covs = {}
        self.ICMparams = {}
        # different icms.
        self.icms = np.zeros(n, dtype='object')
        for i in range(n):
            self.icms[i] = icm(number_outputs, self.sample_locations)
        _BaseHMM.__init__(self, n, None, precision, verbose)
        self.reset()

    def pack_params(self, params_dict):
        # Note: reestimate parameters has to work with self.ICMparams
        # and in the optimization process you should work with the flattened
        # array.
        rbf_vars = params_dict['rbf_variances']
        rbf_lengthscales = params_dict['rbf_lengthscales']
        Ws = params_dict['Ws']
        kappas = params_dict['kappas']
        noise_var = params_dict['noise_var']
        packed_params = []
        for i in range(self.n):
            tmp = np.array([rbf_vars[i], rbf_lengthscales[i]])
            p = np.concatenate((tmp, Ws[i, :], kappas[i, :]))
            packed_params.append(p)
        packed_params = np.concatenate(packed_params)
        packed_params = np.concatenate((packed_params, noise_var))
        return packed_params


    def unpack_params(self, params_array):
        ret_dict = {}
        rbf_variances = np.zeros(self.n)
        rbf_lengthscales = np.zeros(self.n)
        Ws = np.zeros((self.n, self.number_outputs))
        kappas = np.zeros((self.n, self.number_outputs))
        noise_var = np.zeros(self.number_outputs)
        idx = 0
        for i in range(self.n):
            rbf_variances[i] = params_array[idx]
            idx += 1
            rbf_lengthscales[i] = params_array[idx]
            idx += 1
            for j in range(self.number_outputs):
                Ws[i][j] = params_array[idx]
                idx += 1
            for j in range(self.number_outputs):
                kappas[i][j] = params_array[idx]
                idx += 1
        for j in range(self.number_outputs):
            noise_var[j] = params_array[idx]
            idx += 1
        assert idx == np.size(params_array)
        ret_dict['rbf_variances'] = rbf_variances
        ret_dict['rbf_lengthscales'] = rbf_lengthscales
        ret_dict['Ws'] = Ws
        ret_dict['kappas'] = kappas
        ret_dict['noise_var'] = noise_var
        return ret_dict


    def reset(self,init_type='uniform', emissions_reset=True):
        """If required, initialize the model parameters according the selected
        policy."""
        if init_type == 'uniform':
            pi = np.ones(self.n, dtype=self.precision)*(1.0/self.n)
            A = np.ones((self.n,self.n), dtype=self.precision)*(1.0/self.n)
            new_params = {'A': A, 'pi': pi}
            if emissions_reset:
                ICMparams = {}
                ICMparams['rbf_variances'] = np.ones(self.n)
                ICMparams['rbf_lengthscales'] = np.ones(self.n)
                ICMparams['Ws'] = np.random.randn(self.n, self.number_outputs)
                ICMparams['kappas'] = 0.5*np.ones((self.n, self.number_outputs))
                # Assuming different noises for each output.
                ICMparams['noise_var'] = np.ones(self.number_outputs) * 100.
                new_params['ICMparams'] = ICMparams
            else:
                new_params['ICMparams'] = self.ICMparams
            self._updatemodel(new_params)
            if self.observations is not None:
                self._mapB()
        else:
            raise LFMHMMError("reset init_type not supported.")

    def set_params(self, A, pi, rbf_variances, rbf_lengthscales,
                   B_Ws, B_kappas, noise_var):
        n = self.n
        assert A.shape == (n, n)
        assert (pi.shape == (n, 1)) or (pi.shape == (n, ))
        assert len(rbf_variances) == len(rbf_lengthscales) == n
        assert B_Ws.shape == (self.n, self.number_outputs)
        assert B_kappas.shape == (self.n, self.number_outputs)
        assert noise_var.shape == (self.number_outputs,)
        pdict = {}
        pdict['rbf_variances'] = rbf_variances
        pdict['rbf_lengthscales'] = rbf_lengthscales
        pdict['Ws'] = B_Ws
        pdict['kappas'] = B_kappas
        pdict['noise_var'] = noise_var
        self.ICMparams = pdict
        # Setting the transition matrix, the initial state PMF and the emission
        # params.
        params_to_set = {'A': A, 'pi': pi, 'ICMparams': self.ICMparams}
        self._updatemodel(params_to_set)
        if self.observations is not None:
                self._mapB()

    def generate_observations(self, segments, hidden_s=None):
        output = np.zeros((segments,
                           self.locations_per_segment * self.number_outputs),
                           dtype=self.precision)
        initial_state = np.nonzero(np.random.multinomial(1, self.pi))[0][0]
        hidden_states = [initial_state]
        for x in range(1, segments):
            hidden_states.append(np.nonzero(np.random.multinomial(
                    1, self.A[hidden_states[-1]]))[0][0])
        if hidden_s:
            hidden_states = hidden_s
        for i in range(len(hidden_states)):
            state = hidden_states[i]
            cov = self.get_cov_function(state)
            realization = np.random.multivariate_normal(
                    mean=np.zeros(cov.shape[0]), cov=cov)
            output[i, :] = realization
        if self.verbose:
            print("Hidden States", hidden_states)
        return output, hidden_states

    def get_cov_function(self, hidden_state, cache=True):
        if cache and (hidden_state in self.memo_covs):
            return self.memo_covs[hidden_state]
        assert hidden_state < self.n
        cov = self.icms[hidden_state].Kyy()
        self.memo_covs[hidden_state] = cov
        return cov

    def get_cov_function_explicit(self, hidden_state, t, tp):
        # Notice that the noise variance doesn't appear here.
        # The noise variance only affects Ktt.
        assert hidden_state < self.n
        Xt = self.icms[hidden_state].get_stacked_time_and_indexes(
                t, self.number_outputs)
        Xtp = self.icms[hidden_state].get_stacked_time_and_indexes(
                tp, self.number_outputs)
        cov = self.icms[hidden_state].Kff(Xt, Xtp)
        return cov

    def predict(self, t_step, hidden_state, obs):
        if self.verbose and \
                (np.any(t_step < self.start_t) or np.any(t_step > self.end_t)):
            print("WARNING:prediction step.Time step out of the sampling region")
        if hidden_state < 0 or hidden_state >= self.n:
            raise LFMHMMError("ERROR: Invalid hidden state.")
        obs = obs.reshape((-1, 1))
        Ktt = self.get_cov_function(hidden_state)
        ktstar = self.get_cov_function_explicit(
                hidden_state, self.sample_locations, np.asarray(t_step))
        Kstarstar = self.get_cov_function_explicit(
                hidden_state, np.asarray(t_step),  np.asarray(t_step))
        mean_pred = np.dot(ktstar.T, np.linalg.solve(Ktt, obs))
        cov_pred = Kstarstar - np.dot(ktstar.T, np.linalg.solve(Ktt, ktstar))
        return mean_pred, cov_pred

    def _reestimateICMparams(self, gammas):
        new_ICMparams = self.optimize_hyperparams(gammas)
        print("CURRENT VALUE OF EMISSION PARAMS: ")
        print(self.unpack_params(new_ICMparams))
        return self.unpack_params(new_ICMparams)

    def objective_for_hyperparameters(self, gammas, parallelized=True):
        if parallelized:
            return self.parallel_hyperparameters(gammas)
        # non-parallel code
        weighted_sum = 0.0
        n_sequences = len(gammas)
        for i in range(self.n):
            # print "HIDDEN STATE:", i
            current_lfm = self.icms[i]
            cov = self.get_cov_function(i, False)
            mvg = stats.multivariate_normal(np.zeros(cov.shape[0]), cov, True)
            for s in range(n_sequences):
                gamma = gammas[s]
                n_observations = len(gamma)
                for t in range(n_observations):
                    # current_lfm.set_outputs(self.observations[s][t])
                    # weighted_sum += gamma[t][i] * current_lfm.LLeval()
                    # other way
                    weighted_sum += gamma[t][i] * mvg.logpdf(self.observations[s][t])
        return weighted_sum

    def parallel_hyperparameters(self, gammas):
        n_sequences = len(gammas)
        mvgs = []
        for i in range(self.n):
            cov = self.get_cov_function(i, False)
            mvg = stats.multivariate_normal(np.zeros(cov.shape[0]), cov, True)
            mvgs.append(mvg)
        d = {
            'mvgs': mvgs,
            'obs': self.observations,
            'gammas': gammas,
        }
        l = list(zip(list(range(n_sequences)), [d] * n_sequences))
        ret = self.pool.map(_parallel_hyperparameter_objective, l)
        return np.sum(ret)


    def _wrapped_objective(self, params, gammas):
        # print "parameters :", self.unpack_params(params)
        self._update_emission_params(params)
        return -self.objective_for_hyperparameters(gammas)

    def _reestimate(self, stats):
        new_model = _BaseHMM._reestimate(self, stats)
        new_model['ICMparams'] = self._reestimateICMparams(stats['gammas'])
        return new_model

    def optimize_hyperparams(self, gammas):
        # initilization with the current ICMparams.
        packed = self.pack_params(self.ICMparams)
        result = optimize.minimize(self._wrapped_objective, packed, gammas,
                                   bounds=self._get_bounds())
        print("===============")
        print(result.message)
        print("iterations:", result.nit)
        print("===============")
        return result.x

    def _update_emission_params(self, params):
        # Notice that this function works with the packed params.
        # Be careful because this function doesn't update self.ICMparams
        # So it is expected to update it  after/before using this.
        per_hidden_state = 2 + 2*self.number_outputs
        noise_params = params[per_hidden_state * self.n:]
        assert np.size(noise_params) == self.number_outputs
        # updating each of the hidden states with the new params.
        for i in range(self.n):
            icm_params = params[i*per_hidden_state: (i + 1)*per_hidden_state]
            self.icms[i].set_params(
                    np.concatenate((icm_params, noise_params)))

    def _get_bounds(self):
        lower = 1e-8
        bounds = []
        for i in range(self.n):
            f = [(lower, None)] * 2  # rbf variance & length-scale.
            s = [(None, None)] * self.number_outputs  # W bounds.
            t = [(lower, None)] * self.number_outputs  # kappa bounds.
            bounds.extend(f)
            bounds.extend(s)
            bounds.extend(t)
        for i in range(self.number_outputs):
            bounds.append((lower, None))  # noise variance bounds.
        total_length = (2 + 2 * self.number_outputs) * self.n + self.number_outputs
        assert len(bounds) == total_length
        return bounds

    def _updatemodel(self, new_model):
        '''
        This function updates the values of model attributes. Namely
        self.ICMparams, self.A, self.pi and self.icms.
        Note that this doesn't update the probabilities B_maps
        '''
        self.ICMparams = new_model['ICMparams']
        packed_params = self.pack_params(self.ICMparams)
        self._update_emission_params(packed_params)
        _BaseHMM._updatemodel(self, new_model)

    def _mapB(self):
        '''
        Required implementation for _mapB. Refer to _BaseHMM for more details.
        '''
        # Erasing the covariance cache here. Should I do the same in other
        # places ?
        self.memo_covs = {}
        if self.observations is None:
            raise LFMHMMError("The training sequences haven't been set.")

        numbers_of_sequences = len(self.observations)

        self.B_maps = np.zeros((numbers_of_sequences, self.n), dtype=object)

        for j in range(numbers_of_sequences):
            for i in range(self.n):
                self.B_maps[j][i] = np.zeros(len(self.observations[j]),
                                             dtype=self.precision)

        # strange behavior found between numpy and stats. See below.

        for j in range(numbers_of_sequences):
            number_of_segments = len(self.observations[j])
            for i in range(self.n):
                cov = self.get_cov_function(i)
                # print "cond:", np.linalg.cond(cov)
                for t in range(number_of_segments):
                    self.B_maps[j][i][t] = stats.multivariate_normal.pdf(
                            self.observations[j][t], np.zeros(cov.shape[0]),
                            cov, True)  # Allowing singularity in cov sometimes.
                    #  This is weird.
        # print self.B_maps

    def save_params(self, dir, name):
        f = file('%s/%s.param' % (dir, name), 'w')
        f.write(repr(self.ICMparams) + "\n")
        f.write("#\n")
        f.write(repr(self.A) + "\n")
        f.write("#\n")
        f.write(repr(self.pi) + "\n")
        f.close()

    def read_params(self, dir, name):
        f = file('%s/%s.param' % (dir, name), 'r')
        ICMparams_string = ""
        A_string = ""
        pi_string = ""
        read_line = f.readline()
        while not read_line.startswith("#"):
            ICMparams_string += read_line
            read_line = f.readline()
        read_line = f.readline()
        while not read_line.startswith("#"):
            A_string += read_line
            read_line = f.readline()
        read_line = f.readline()
        while read_line:
            pi_string += read_line
            read_line = f.readline()
        f.close()
        from numpy import array, nan  # required for eval to work.
        model_to_set = {
            'ICMparams': eval(ICMparams_string),
            'A': eval(A_string),
            'pi': eval(pi_string),
        }
        assert model_to_set['A'].shape == (self.n, self.n)
        assert np.size(model_to_set['pi']) == self.n
        assert model_to_set['ICMparams']['rbf_variances'].shape == \
               model_to_set['ICMparams']['rbf_lengthscales'].shape == \
               (self.n,)
        assert model_to_set['ICMparams']['Ws'].shape ==\
               model_to_set['ICMparams']['kappas'].shape ==\
               (self.n, self.number_outputs)
        assert model_to_set['ICMparams']['noise_var'].shape == \
               (self.number_outputs,)
        self._updatemodel(model_to_set)
        if self.observations is not None:
            self._mapB()

