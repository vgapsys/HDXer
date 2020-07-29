#!/usr/bin/env python

import numpy as np
import argparse, os, sys
from glob import glob
from copy import deepcopy
import pickle

from .errors import HDX_Error
from .reweighting_functions import read_contacts_hbonds, read_kints_segments

class MaxEnt():
    """Class for Maximum Entropy reweighting of a predicted
       HDX ensemble to fit target data. Initialises with a 
       dictionary of default parameters foe analysis, accessible
       as MaxEnt.params

       Default parameters can either be updated directly in the MaxEnt.params
       dictionary or by supplying extra parameters as kwargs during
       initialisation, e.g. MaxEnt(gamma=10**-2) or MaxEnt(**param_dict)

       Perform a Maximum Entropy reweighting of an ensemble of
       predicted HDX data using the MaxEnt.run method"""

    def __init__(self, **extra_params):
        """Initialize a Maximum Entropy reweighting object with the following parameters.
           Syntax of this documentation = Parameter (default value) : Description
               do_reweight (True) : Flag to turn on reweighting
               do_params (True) : Flag to turn on model parameter optimisation (e.g. Bc and Bh)
               do_mcmin (False) : Flag to turn on a Monte Carlo minimisation of model parameters, rather than a gradient-based optimisation
               do_mcsampl (False) : Flag to turn on a Monte Carlo sampling of model parameters, rather than a minimisation
               mc_refvar (0.03) : Reference variance of the MC sampling distribution (~estimated error on deuterated fractions)
               mc_equilsteps (-1) : Equilibration steps and subsequent smoothing (as 1/sqrt(number of steps)) for the initial stage of MC sampling. Default value (-1) switches off equilibration
               radou_bc (0.35) : Initial value of beta_C for the Radou-style HDX forward model
               radou_bh (2.00) : Initial value of beta_H for the Radou-style HDX forward model
               radou_bcrange (1.5) : Initial range (affects model parameter step size) for the beta_C parameter for MC minimization
               radou_bhrange (16.0) : Initial range (affects model parameter step size) for the beta_H parameter for MC minimization
               tolerance (10**-10) : Convergence criterion for reweighting, taken as: ( sum(abs(lambdas_new) - sum(abs(lambdas_old)) ) / sum(abs(lambdas_new))
               maxiters (10**6) : Maximum number of iterations for reweighting
               param_maxiters (10**2) : Maximum number of iterations for model parameter minimisation
               stepfactor (10**-5) : Initial rate (affects lambdas step size) for reweighting
               stepfactor_scaling (1.005) : Ratio for scaling down of initial step size if oscillations are observed
               param_stepfactor (10**-1) : Initial rate (affects model parameter step size) for MC parameter optimisation
               temp (300.) : Temperature for HDX predictions in Kelvin
               random_initial (False) : Randomize the initial frame weights. Otherwise, all frames will initially be weighted equally"""

        maxentparams = { 'do_reweight' : True,
                         'do_params' : True,
                         'do_mcmin' : False,
                         'do_mcsampl' : False,
                         'mc_refvar' : 0.03,
                         'mc_equilsteps' : -1,
                         'radou_bc' : 0.35,
                         'radou_bh' : 2.00,
                         'radou_bcrange' : 1.5,
                         'radou_bhrange' : 16.0,
                         'tolerance' : 10**-10,
                         'maxiters' : 10**6,
                         'param_maxiters' : 10**2,
                         'stepfactor' : 10**-5,
                         'stepfactor_scaling' : 1.005,
                         'param_stepfactor' : 10**-1,
                         'temp' : 300.,
                         'random_initial' : False
                         }
        maxentparams.update(extra_params)
        maxentparams.update(kT = maxentparams['temp'] * 0.008314598)
        self.methodparams = maxentparams



    # Setup functions
    def setup_no_runobj(self, folderlist, kint_file, expt_file_path, times):
        """Setup initial variables when a run object file is NOT read in"""
        self.runvalues = {}

        maxentvalues = { 'contacts' : None,
                         'hbonds' : None,
                         'kint' : None,
                         'exp_dfrac' : None,
                         'segfilters' : None,
                         'lambdas' : None,
                         'nframes' : None,
                         'iniweights' : None,
                         'minuskt_filtered' : None,
                         'exp_dfrac_filtered' : None,
                         'n_datapoints' : None,
                         'n_segs' : None,
                         'lambdamod' : None,
                         'deltalambdamod' : None,
                         'curriter' : None }
        _contacts, _hbonds, _sorted_resids = read_contacts_hbonds(folderlist,
                                                                  self.runparams['contacts_prefix'],
                                                                  self.runparams['hbonds_prefix'])
        _nresidues = len(_hbonds)
        _minuskt, _exp_dfrac, _segfilters = read_kints_segments(kint_file, expt_file_path, _nresidues, times, _sorted_resids)

        # Starting lambda values
        _lambdas = np.zeros(_nresidues)

    #    if mc_sample_params:
        if self.methodparams['do_mcsampl']:
            self.mcsamplvalues = {}
            mcsamplvalues = {'final_MClambdas': np.zeros(_nresidues),
                             'final_MClambdas_h': np.zeros(_nresidues),
                             'final_MClambdas_c': np.zeros(_nresidues),
                             'ave_MClambdas_h': np.zeros(_nresidues),
                             'ave_MClambdas_c': np.zeros(_nresidues),
                             'lambdas_c': np.zeros(_nresidues),
                             'lambdas_h': np.zeros(_nresidues),
                             'MC_MSE_ave': 0.,
                             'MC_resfracs_ave': 0.}
            self.mcsamplvalues.update(mcsamplvalues)

        # Write initial parameter values
        _nframes = _contacts.shape[1]
        with open("%sinitial_params.dat" % self.runparams['out_prefix'], 'w') as f:
            f.write("Temp, kT, convergence tolerance, BetaC, BetaH, gamma, update rate (step size) factor, nframes\n")
            f.write("%s, %6.3f, %5.2e, %5.2f, %5.2f, %5.2e, %8.6f, %d\n"
                    % (self.methodparams['temp'],
                       self.methodparams['kT'],
                       self.methodparams['tolerance'],
                       self.methodparams['radou_bc'],
                       self.methodparams['radou_bh'],
                       self.runparams['gamma'],
                       self.methodparams['stepfactor'],
                       _nframes))

        # Random initial weights perturb the ensemble - e.g. can we recreate the final ensemble obtained with equally weighted frames?
        # Seed for random state will be printed out in logfile for reproducibility
        if self.methodparams['random_initial']:
            np.random.seed(None)
            statenum = np.random.randint(2**32)
            state = np.random.RandomState(statenum)
            with open("%sinitial_params.dat" % self.runparams['out_prefix'], 'a') as f:
                f.write('Initial weights were randomized, seed for np.random.RandomState = %d\n' % statenum)
            _iniweights = state.rand(_nframes)
            np.savetxt("initial_weights_RandomState%d.dat" % statenum, _iniweights)
            np.random.seed(None)
        else:
            _iniweights = np.ones(_nframes)

        # Write some headers in output files
        with open("%swork.dat" % self.runparams['out_prefix'], 'w') as f:
            f.write("# gamma, chisquare, work(kJ/mol)\n")
        with open("%sper_iteration_output.dat" % self.runparams['out_prefix'], 'w') as f:
            f.write("# Iteration, avehdxdev, chisquare, lambdamod, deltalambdamod, rate, Bh, Bc \n")
        with open("%sper_restart_output.dat" % self.runparams['out_prefix'], 'w') as f:
            f.write("# Iteration, avehdxdev, chisquare, lambdamod, deltalambdamod, rate, Bh, Bc, work \n")

        # Set some constants & initial values for the iteration loop
        _minuskt_filtered = -_minuskt * _segfilters
        _exp_dfrac_filtered = _exp_dfrac * _segfilters
        _n_datapoints = np.sum(_segfilters)
        _n_segs = _segfilters.shape[0]
        _lambdamod = 0.0
        _deltalambdamod = 0.0
        _curriter = 0

        maxentvalues.update(contacts = _contacts,
                            hbonds = _hbonds,
                            minuskt = _minuskt,
                            exp_dfrac = _exp_dfrac,
                            segfilters = _segfilters,
                            lambdas = _lambdas,
                            nframes = _nframes,
                            iniweights = _iniweights,
                            minuskt_filtered = _minuskt_filtered,
                            exp_dfrac_filtered = _exp_dfrac_filtered,
                            n_datapoints = _n_datapoints,
                            n_segs = _n_segs,
                            lambdamod = _lambdamod,
                            deltalambdamod = _deltalambdamod,
                            curriter = _curriter)

        self.runvalues.update(maxentvalues)

    def setup_runobj(self, runobj):
        """Setup initial variables when a run object file IS read in"""

    def setup_restart(self, rstfile):
        """Setup initial variables when a restart file IS read in"""
        # List of pickle variables:
        global contacts, hbonds, kint, exp_dfrac, segfilters, lambdas, \
               lambdasc, lambdash, _lambdanewaverh, _lambdanewaverc, \
               _lambdanew, _lambdanewh, _lambdanewc, chisquareav, \
               nframes, iniweights, num, exp_df_segf, normseg, numseg, lambdamod, deltalambdamod, \
               Bc, Bh, gamma, ratef, avesigmalnpi, currcount
        contacts, hbonds, kint, exp_dfrac, segfilters, lambdas,lambdasc ,lambdash , _lambdanewaverh, _lambdanewaverc, \
        _lambdanew, _lambdanewh, _lambdanewc, chisquareav, \
        nframes, iniweights, num, exp_df_segf, normseg, numseg, lambdamod, deltalambdamod, \
        Bc, Bh, gamma, ratef, avesigmalnpi, currcount = pickle.load(open(rstfile, 'rb'))
        # Append to existing files
        with open("%sinitial_params.dat" % self.runparams['out_prefix'], 'a') as f:
            f.write("# RESTARTED FROM FILE %s :\n" % rstfile)
            f.write("Temp, kT, convergence tolerance, BetaC, BetaH, gamma, update rate (step size) factor, nframes\n")
            f.write("%s, %6.3f, %5.2e, %5.2f, %5.2f, %5.2e, %8.6f, %d\n" % (T, kT, tol, Bc, Bh, gamma, ratef, nframes))
        with open("%swork.dat" % self.runparams['out_prefix'], 'a') as f:
            f.write("# RESTARTED FROM FILE %s :\n" % rstfile)
            f.write("# gamma, chisquare, work(kJ/mol)\n")
        with open("%sper_iteration_output.dat" % self.runparams['out_prefix'], 'a') as f:
            f.write("# RESTARTED FROM FILE %s :\n" % rstfile)
            f.write("# Iteration, avehdxdev, chisquare, lambdamod, deltalambdamod, rate, Bh, Bc \n")
        with open("%sper_restart_output.dat" % self.runparams['out_prefix'], 'a') as f:
            f.write("# RESTARTED FROM FILE %s :\n" % rstfile)
            f.write("# Iteration, avehdxdev, chisquare, lambdamod, deltalambdamod, rate, Bh, Bc, work \n")
        print("Restart file %s read" % rstfile)

    def set_run_params(self, gamma, runobj, restart, paramdict):
        """Set basic run parameters if not being read from calc_hdx object or a restart file"""

        self.runparams = {}
        self.runparams['gamma'] = gamma

        if restart is not None:
            self.runparams['from_restart'] = True
            return
        if runobj is not None:
            self.runparams['from_calchdx'] = True
            return

        self.runparams.update(paramdict) # update with any provided options

        # If options aren't provided, set defaults
        try:
            self.runparams['hbonds_prefix']
        except KeyError:
            self.runparams['hbonds_prefix'] = 'Hbonds_'
        try:
            self.runparams['contacts_prefix']
        except KeyError:
            self.runparams['contacts_prefix'] = 'Contacts_'
        try:
            self.runparams['out_prefix']
        except KeyError:
            self.runparams['out_prefix'] = 'reweighting_'

    def update_lnpi_and_weights(self):
        """Update the current values of ensemble-averaged protection factor for each residue,
           and current weight for each trajectory frame,
           based on the current lambda (biasing) value for each residue.

           Updates 'lnpi', 'avelnpi' and 'currweights' entries in the MaxEnt.runvalues dictionary"""

        _contacts = self.methodparams['radou_bc'] * self.runvalues['contacts']
        _hbonds = self.methodparams['radou_bh'] * self.runvalues['hbonds']
        self.runvalues['lnpi'] = _hbonds + _contacts

        # Calculate by-frame bias from lambda values (per residue) and contacts/hbonds (per residue and frame)
        if self.methodparams['do_mcsampl']:
            biasfactor = np.sum(self.mcsamplvalues['lambdas_c'][:, np.newaxis] * self.runvalues['contacts']
                                + self.mcsamplvalues['lambdas_h'][:, np.newaxis] * self.runvalues['hbonds'],
                                axis=0)  # Sum over all residues, = array of len(nframes). lambdas is 1D array broadcast to 2D
        else:
            biasfactor = np.sum(self.runvalues['lambdas'][:, np.newaxis] * self.runvalues['lnpi'],
                                axis=0)  # Sum over all residues, = array of len(nframes). lambdas is 1D array broadcast to 2D

        # Calculate current weight of each frame (from initial weight & bias applied), and weighted-average protection factors (by-residue)
        self.runvalues['currweights'] = self.runvalues['iniweights'] * np.exp(biasfactor)
        self.runvalues['currweights'] = self.runvalues['currweights'] / np.sum(self.runvalues['currweights'])
        self.runvalues['ave_lnpi'] = np.sum(self.runvalues['currweights'] * self.runvalues['lnpi'], axis=1)

        # Calculate helper values & convert weighted-average protection factors to 3 dimensions.
        # On first iteration, set std. dev. of ln(Pf)
        if self.runvalues['curriter'] == 1:
            _sigmalnpi = np.sum(self.runvalues['currweights'] * (self.runvalues['lnpi']**2), axis=1)
            _sigmalnpi = _sigmalnpi - (self.runvalues['ave_lnpi']**2)
            self.runvalues['sigma_lnpi'] = np.sqrt(_sigmalnpi)
            self.runvalues['ave_sigma_lnpi'] = np.mean(self.runvalues['sigma_lnpi'])

        # Convert weighted-average protection factors to 3D arrays of shape [n_segments, n_residues, n_times]
        # This is the same shape as self.runvalues['segfilters'] used to filter arrays by residues belonging to each segment
        self.runvalues['ave_lnpi'] = np.repeat(self.runvalues['ave_lnpi'][:,np.newaxis], len(self.runparams['times']), axis=1)
        self.runvalues['ave_lnpi'] = self.runvalues['ave_lnpi'][np.newaxis,:,:].repeat(self.runvalues['n_segs'], axis=0)

    def update_dfracs_and_mse(self):
        """Convert weighted-average protection factors to deuterated fractions
           and calculate mean square error (MSE) and RMSE to experiment.

           Updates 'curr_residue_dfracs', 'curr_segment_dfracs' and 'curr_MSE' entries in the MaxEnt.runvalues dictionary"""

        # Do arithmetic always multiplying values by filter, so 'False' entries are not counted
        # Set temporary array for denominator so we can use np.divide to avoid divide-by-zero warning
        denom = self.runvalues['ave_lnpi'] * self.runvalues['segfilters']
        # D(t) = 1 - exp(-kt/P_i)
        self.runvalues['curr_residue_dfracs'] = 1.0 - \
                                                np.exp(np.divide(self.runvalues['minuskt_filtered'], np.exp(denom),
                                                                 out=np.full(self.runvalues['minuskt_filtered'].shape, np.nan),
                                                                 where=denom!=0))

        _curr_segment_dfracs = np.nanmean(self.runvalues['curr_residue_dfracs'], axis=1)
        # Convert to 3D array of shape [n_segments, n_residues, n_times]
        self.runvalues['curr_segment_dfracs'] = _curr_segment_dfracs[:,np.newaxis,:].repeat(self.runvalues['segfilters'].shape[1], axis=1)
        # Although curr_segment_dfracs is a 3D array, repeated for every residue in a segment, dividing by n_datapoints
        # should give a mean square error that's weighted by the number of residues in each segment
        self.runvalues['curr_MSE'] = np.sum((self.runvalues['curr_segment_dfracs'] * self.runvalues['segfilters']
                                             - self.runvalues['exp_dfrac_filtered'])**2) / self.runvalues['n_datapoints']

    def sample_parameters_MC(self):
        """Sample range of beta parameters using an MC protocol.

           If sampling is switched on using by setting MaxEnt.methodparams['do_mcsampl'] = True,
           then parameter sampling is controlled by the following dictionary entries:
           MaxEnt.methodparams['mc_equilsteps'] : Equilibration steps and subsequent smoothing (as 1/sqrt(number of steps)) for the initial stage of MC sampling. Default value (-1) switches off equilibration
           MaxEnt.methodparams['param_maxiters'] :  Maximum number of iterations for model parameter minimisation
           MaxEnt.methodparams['mc_refvar'] : Reference variance of the MC sampling distribution (~estimated error on deuterated fractions)

           The current values for the sampling are stored in the MaxEnt.mcsamplvalues dictionary

           The final result of MC parameter sampling updates radou_bc and radou_bh in the MaxEnt.methodparams dictionary"""

        # Define some helpful internal functions for making MC moves and recalculating protection factors & deuterated fractions
        def update_sampled_totals(total_bc, total_bh, total_mse, total_resfracs, total_lambdas_bc, total_lambdas_bh):
            # Add current values of bc, bh, mse, residue fractions, lambdas to totals using a closure.
            def adding_function(bc, bh, mse, resfracs, lambdas):
                # Creates function to add current values to totals
                total_bc += bc
                total_bh += bh
                total_mse += mse
                total_resfracs += resfracs
                total_lambdas_bc += bc * lambdas
                total_lambdas_bh += bh * lambdas
                return total_bc, total_bh, total_mse, total_resfracs, total_lambdas_bc, total_lambdas_bh
            return adding_function

        def update_sampled_totals_nolambda(total_bc, total_bh, total_mse, total_resfracs):
            # Add current values of bc, bh, mse, residue fractions to totals using a closure.
            def adding_function(bc, bh, mse, resfracs):
                # Creates function to add current values to totals
                total_bc += bc
                total_bh += bh
                total_mse += mse
                total_resfracs += resfracs
                return total_bc, total_bh, total_mse, total_resfracs
            return adding_function

        def update_sampled_averages(total_bc, total_bh, total_mse, total_resfracs, total_lambdas_bc, total_lambdas_bh):
            # Turns totals of bc, bh mse, fractions, lambdas into averages
            ave_bc = total_bc / self.methodparams['param_maxiters']
            ave_bh = total_bh / self.methodparams['param_maxiters']
            ave_mse = total_mse / self.methodparams['param_maxiters']
            ave_resfracs = total_resfracs / self.methodparams['param_maxiters']
            ave_lambdas_bc = total_lambdas_bc / self.methodparams['param_maxiters']
            ave_lambdas_bh = total_lambdas_bh / self.methodparams['param_maxiters']
            return ave_bc, ave_bh, ave_mse, ave_resfracs, ave_lambdas_bc, ave_lambdas_bh

        def update_sampled_averages_nolambda(total_bc, total_bh, total_mse, total_resfracs):
            # Turns totals of bc, bh mse, fractions, lambdas into averages
            ave_bc = total_bc / self.methodparams['param_maxiters']
            ave_bh = total_bh / self.methodparams['param_maxiters']
            ave_mse = total_mse / self.methodparams['param_maxiters']
            ave_resfracs = total_resfracs / self.methodparams['param_maxiters']
            return ave_bc, ave_bh, ave_mse, ave_resfracs

        def generate_trial_betas(bc, bh):
            # Make move in betas scaled by step size and desired 'range' of sampling. -ve beta values are not allowed
            trial_radou_bh, trial_radou_bc = -1, -1
            while trial_radou_bh < 0:
                trial_radou_bh = bh + ((np.random.random_sample()) - 0.5) \
                                 * self.methodparams['param_stepfactor'] * self.methodparams['radou_bhrange']
            while trial_radou_bc < 0:
                trial_radou_bc = bc + ((np.random.random_sample()) - 0.5) \
                                 * self.methodparams['param_stepfactor'] * self.methodparams['radou_bcrange']
            return bc, bh

        def calc_trial_ave_lnpi(ave_contacts, ave_hbonds, bc, bh):
            # recalculate ave_lnpi with the given parameters & broadcast to the usual 3D array of [n_segments, n_residues, n_times]
            trial_ave_lnpi = (bc * ave_contacts) + (bh * ave_hbonds)

            trial_ave_lnpi = np.repeat(trial_ave_lnpi[:, np.newaxis], len(self.runparams['times']), axis=1)
            trial_ave_lnpi = trial_ave_lnpi[np.newaxis, :, :].repeat(self.runvalues['n_segs'], axis=0)
            return trial_ave_lnpi

        def calc_trial_dfracs(ave_lnpi):
            # recalculate the deuterated fractions and MSE with the given ave_lnpi
            denom = ave_lnpi * self.runvalues['segfilters']
            residue_dfracs = 1.0 - \
                             np.exp(np.divide(self.runvalues['minuskt_filtered'], np.exp(denom),
                                              out=np.full(self.runvalues['minuskt_filtered'].shape, np.nan),
                                              where=denom != 0))

            segment_dfracs = np.nanmean(residue_dfracs, axis=1)
            segment_dfracs = segment_dfracs[:, np.newaxis, :].repeat(self.runvalues['segfilters'].shape[1],
                                                                             axis=1)
            MSE = np.sum((segment_dfracs * self.runvalues['segfilters']
                          - self.runvalues['exp_dfrac_filtered']) ** 2) / self.runvalues['n_datapoints']
            return residue_dfracs, segment_dfracs, MSE

        def calc_lambdas_in_sampleloop(ave_lnpi, segment_dfracs):
            # Calculate values of lambda during middle of MC sampling loop to add to averages
            denom = ave_lnpi * self.runvalues['segfilters']
            curr_lambdas = np.nansum(np.sum((segment_dfracs * self.runvalues['segfilters'] - self.runvalues['exp_dfrac_filtered']) * \
                                     np.exp(np.divide(self.runvalues['minuskt_filtered'], np.exp(denom), out=np.full(self.runvalues['minuskt_filtered'].shape, np.nan),
                                                      where=denom != 0)) * \
                                     np.divide(-self.runvalues['minuskt_filtered'], np.exp(denom), out=np.full(self.runvalues['minuskt_filtered'].shape, np.nan),
                                               where=denom != 0), axis=2) / \
                                   (np.sum(segfilters, axis=1)[:, 0])[:, np.newaxis], axis=0)
            return curr_lambdas

        ### End of useful functions
        ### Start of sampling code
        # First, determine if we want to do some equilibration steps, and set the smoothing applied to the step size during equilibration
        if self.methodparams['mc_equilsteps'] > 0:
            smoothing_rate = self.methodparams['mc_equilsteps'] \
                             / np.sqrt(self.methodparams['mc_equilsteps']
                             * (self.methodparams['mc_equilsteps'] + self.runvalues['curriter']))
        else:
            smoothing_rate = 1.0

        # Get the weighted-average contacts & H-bonds for each residue
        _ave_contacts = np.sum(self.runvalues['currweights'] * self.runvalues['contacts'], axis=1)
        _ave_hbonds = np.sum(self.runvalues['currweights'] * self.runvalues['hbonds'], axis=1)

        # Save original values before sampling if not saved elsewhere
        self.mcsamplvalues['MC_MSE_ave'] = self.runvalues['curr_MSE']
        self.mcsamplvalues['MC_resfracs_ave'] = self.runvalues['curr_residue_dfracs']
        curr_radou_bh = self.methodparams['radou_bh']
        curr_radou_bc = self.methodparams['radou_bc']

        ### Start of main sampling loop
        for curr_mc_iter in range(self.methodparams['param_maxiters']):
            # 1) Make move in betas and recalculate protection factors & deuterated fractions
            trial_radou_bc, trial_radou_bh = generate_trial_betas(curr_radou_bc, curr_radou_bh)
            trial_ave_lnpi = calc_trial_ave_lnpi(_ave_contacts, _ave_hbonds, trial_radou_bc, trial_radou_bh)
            trial_residue_dfracs, trial_segment_dfracs, trial_MSE = calc_trial_dfracs(trial_ave_lnpi)

            # Immediately accept move if it improves MSE to experiment
            if trial_MSE < self.runvalues['curr_MSE']:
                curr_radou_bh = trial_radou_bh
                curr_radou_bc = trial_radou_bc
                self.runvalues['curr_MSE'] = trial_MSE
                self.runvalues['ave_lnpi'] = trial_ave_lnpi
                self.runvalues['curr_segment_dfracs'] = trial_segment_dfracs
                self.runvalues['curr_residue_dfracs'] = trial_residue_dfracs
            else:
                # Acceptance test based on Gaussian distribution of MSDs with variance MaxEnt.methodparams['mc_refvar']
                trial_acc_val = self.runvalues['n_segs'] * len(self.runparams['times']) \
                                * trial_MSE / (2.0 * self.methodparams['mc_refvar'])
                orig_acc_val = self.runvalues['n_segs'] * len(self.runparams['times']) \
                               * self.runvalues['curr_MSE'] / (2.0 * self.methodparams['mc_refvar'])
                move_prob = np.exp(-(trial_acc_val - orig_acc_val))
                if move_prob > np.random.random_sample():
                    curr_radou_bh = trial_radou_bh
                    curr_radou_bc = trial_radou_bc
                    self.runvalues['curr_MSE'] = trial_MSE
                    self.runvalues['ave_lnpi'] = trial_ave_lnpi
                    self.runvalues['curr_segment_dfracs'] = trial_segment_dfracs
                    self.runvalues['curr_residue_dfracs'] = trial_residue_dfracs

            if self.methodparams['do_reweight']:
                curr_lambdas = calc_lambdas_in_sampleloop(self.runvalues['ave_lnpi'], self.runvalues['curr_segment_dfracs'])
                if curr_mc_iter == 0:
                    add_to_totals = update_sampled_totals(0, 0, 0, 0, 0, 0)
                else:
                    add_to_totals = update_sampled_totals(radou_bc_sum, radou_bh_sum, MSE_sum, residue_dfracs_sum, lambdas_bc_sum, lambdas_bh_sum)
                radou_bc_sum, radou_bh_sum, MSE_sum, residue_dfracs_sum, lambdas_bc_sum, lambdas_bh_sum = add_to_totals(curr_radou_bc, curr_radou_bh, self.runvalues['curr_MSE'], self.runvalues['curr_residue_dfracs'], curr_lambdas)
            else:
                if curr_mc_iter == 0:
                    add_to_totals = update_sampled_totals_nolambda(0, 0, 0, 0)
                else:
                    add_to_totals = update_sampled_totals_nolambda(radou_bc_sum, radou_bh_sum, MSE_sum, residue_dfracs_sum)
                radou_bc_sum, radou_bh_sum, MSE_sum, residue_dfracs_sum = add_to_totals(curr_radou_bc, curr_radou_bh, self.runvalues['curr_MSE'], self.runvalues['curr_residue_dfracs'])
            ### End of main sampling loop

        # Calculate averages of the sampled values
        if self.methodparams['do_reweight']:
            radou_bc_ave, radou_bh_ave, MSE_ave, residue_dfracs_ave, self.mcsamplvalues['ave_MClambdas_c'], self.mcsamplvalues['ave_MClambdas_h'] = update_sampled_averages(radou_bc_sum, radou_bh_sum, MSE_sum, residue_dfracs_sum, lambdas_bc_sum, lambdas_bh_sum)
        else:
            radou_bc_ave, radou_bh_ave, MSE_ave, residue_dfracs_ave = update_sampled_averages_nolambda(radou_bc_sum, radou_bh_sum, MSE_sum, residue_dfracs_sum)


        # Finally, update the main dictionaries with averages of Bh, Bc, MSE, lambda etc, applying a scaling factor for the initial equilibration period if desired
        self.methodparams['radou_bh'] = self.methodparams['radou_bh'] * (1.0 - smoothing_rate) + \
                                        smoothing_rate * (radou_bh_ave / self.methodparams['param_maxiters'])
        self.methodparams['radou_bc'] = self.methodparams['radou_bc'] * (1.0 - smoothing_rate) + \
                                        smoothing_rate * (radou_bc_ave / self.methodparams['param_maxiters'])
        self.mcsamplvalues['MC_MSE_ave'] = self.mcsamplvalues['MC_MSE_ave'] * (1.0 - smoothing_rate) + \
                                           smoothing_rate * (MSE_ave / self.methodparams['param_maxiters'])
        self.mcsamplvalues['MC_resfracs_ave'] = self.mcsamplvalues['MC_resfracs_ave'] * (1.0 - smoothing_rate) + \
                                           smoothing_rate * (residue_dfracs_ave / self.methodparams['param_maxiters']) # Not currently used but could be - average of residue fractions

        # Update lambdas using final Bh & Bc values if desired
        if self.methodparams['do_reweight']:
            _lambdanewh = self.mcsamplvalues['final_MClambdas_h'] * (1.0 - smoothing_rate) + \
                          smoothing_rate * (self.mcsamplvalues['ave_MClambdas_h'] / self.methodparams['param_maxiters'])
            _lambdanewc = self.mcsamplvalues['final_MClambdas_c'] * (1.0 - smoothing_rate) + \
                          smoothing_rate * (self.mcsamplvalues['ave_MClambdas_c'] / self.methodparams['param_maxiters'])
            _lambdanew = 0.5 * ((_lambdanewh / self.methodparams['radou_bh']) + (_lambdanewc / self.methodparams['radou_bc']))
            self.mcsamplvalues['final_MClambdas_h'] = self.runparams['gamma'] * _lambdanewh
            self.mcsamplvalues['final_MClambdas_c'] = self.runparams['gamma'] * _lambdanewc
            self.mcsamplvalues['final_MClambdas'] = 0.5 * ((self.mcsamplvalues['final_MClambdas_h'] / self.methodparams['radou_bh']) +
                                                           (self.mcsamplvalues['final_MClambdas_c'] / self.methodparams['radou_bc']))

            # Calc example stepsize & make move in lambdas
            ave_deviation = np.sum(np.abs(_lambdanew)) / np.sum(self.mcsamplvalues['final_MClambdas'] != 0)
            self.runvalues['curr_lambda_stepsize'] = self.methodparams['stepfactor'] / (self.runparams['gamma'] * ave_deviation * self.runvalues['ave_sigma_lnpi']) # Example stepsize based on ave_sigma_lnpi
            self.mcsamplvalues['lambdas_c'] = self.mcsamplvalues['lambdas_c'] * (1.0 - self.runvalues['curr_lambda_stepsize']) + \
                                              (self.runvalues['curr_lambda_stepsize'] * self.mcsamplvalues['final_MClambdas_c'])
            self.mcsamplvalues['lambdas_h'] = self.mcsamplvalues['lambdas_h'] * (1.0 - self.runvalues['curr_lambda_stepsize']) + \
                                              (self.runvalues['curr_lambda_stepsize'] * self.mcsamplvalues['final_MClambdas_h'])
            self.runvalues['lambdas'] = self.runvalues['lambdas'] * (1.0 - self.runvalues['curr_lambda_stepsize']) + \
                                            (self.runvalues['curr_lambda_stepsize'] * self.mcsamplvalues['final_MClambdas'])


    def run(self, gamma=10**-2, runobj=None, restart=None, **run_params):
        self.set_run_params(gamma, runobj, restart, run_params)

        # Choose which setup to do. Restart > Runobj > Normal
        if restart is None:
            if runobj is None:
                try:
                    self.setup_no_runobj(self.runparams['data_folders'],
                                         self.runparams['kint_file'],
                                         self.runparams['exp_file'],
                                         self.runparams['times'])
                except KeyError:
                    raise HDX_Error("Missing parameters to set up a reweighting run.\n"
                                    "Please ensure a restart or calc_hdx object is provided,"
                                    "or provide the following arguments to the run() call:"
                                    "data_folders, kint_file, exp_file, times")
            else:
                self.setup_runobj(runobj)
        else:
            self.setup_restart(restart)

        # 1) Do setup (restart or no restart)
        # 2) Do run 
        # 3) Do final save/cleanup


