# PrufPlantAnalysis
#
# This class defines key analytical routines for the PRUF/WRA Benchmarking
# standard operational assessment.
#
# The PrufPlantAnalysis object is a factory which instantiates either the Pandas, Dask, or Spark
# implementation depending on what the user prefers.
#
# The resulting object is loaded as a plugin into each PlantData object.

import random
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import statsmodels.api as sm
from tqdm import tqdm
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import KFold
from operational_analysis.toolkits import met_data_processing as mt
from operational_analysis.toolkits import timeseries as tm
from operational_analysis.toolkits.machine_learning_setup import MachineLearningSetup
from operational_analysis.toolkits import unit_conversion as un
from operational_analysis.toolkits import filters
from operational_analysis.types import timeseries_table
from operational_analysis import logged_method_call
from operational_analysis import logging

logger = logging.getLogger(__name__)


class MonteCarloAEP(object):
    """
    A serial (Pandas-driven) implementation of the benchmark PRUF operational
    analysis implementation. This module collects standard processing and
    analysis methods for estimating plant level operational AEP and uncertainty.

    The preprocessing should run in this order:

        1. Process revenue meter energy - creates monthly/daily data frame, gets revenue meter on monthly/daily basis, and adds
           data flag
        2. Process loss estimates - add monthly/daily curtailment and availabilty losses to monthly/daily data frame
        3. Process reanalysis data - add monthly/daily density-corrected wind speeds, temperature (if used) and wind direction (if used)
            from several reanalysis products to the monthly data frame
        4. Set up Monte Carlo - create the necessary Monte Carlo inputs to the OA process
        5. Run AEP Monte Carlo - run the OA process iteratively to get distribution of AEP results

    The end result is a distribution of AEP results which we use to assess expected AEP and associated uncertainty
    """

    @logged_method_call
    def __init__(self, plant, uncertainty_meter=0.005, uncertainty_losses=0.05,
                 uncertainty_windiness=(10, 20), uncertainty_wind_bin_thresh=(1, 3), 
                 uncertainty_loss_max=(10, 20), uncertainty_max_power_filter=(0.8, 0.9), 
                 uncertainty_nan_energy=0.01, time_resolution = 'M', reg_model = 'lin', 
                 reg_temperature = 'N', reg_winddirection = 'N'):
        """
        Initialize APE_MC analysis with data and parameters.

        Args:
         plant(:obj:`PlantData object`): PlantData object from which PlantAnalysis should draw data.
         uncertainty_meter(:obj:`float`): uncertainty on revenue meter data
         uncertainty_losses(:obj:`float`): uncertainty on long-term losses
         uncertainty_windiness(:obj:`float`): number of years to use for the windiness correction
         uncertainty_wind_bin_thresh(:obj:`float`): The filter threshold for each bin (default is 2 m/s)
         uncertainty_loss_max(:obj:`float`): threshold for the combined availabilty and curtailment monthly loss threshold
         uncertainty_max_power_filter(:obj:`float`): Maximum power threshold (fraction) to which the bin filter 
                                            should be applied (default 0.85)
         uncertainty_nan_energy(:obj:`float`): threshold to flag days/months based on NaNs
         time_resolution(:obj:`string`): whether to perform the AEP calculation at monthly ('M') or daily ('D') time resolution
         reg_model(:obj:`string`): which model to use for the regression ('lin' for linear, 'gam', 'gbm', 'etr')
         reg_temperature(:obj:`string`): whether to include temperature ('Y') or not ('N') as regression input
         reg_winddirection(:obj:`string`): whether to include wind direction ('Y') or not ('N') as regression input

        """
        logger.info("Initializing MonteCarloAEP Analysis Object")

        self._monthly_daily = timeseries_table.TimeseriesTable.factory(plant._engine)
        self._plant = plant  # defined at runtime

        # Memo dictionaries help speed up computation
        self.outlier_filtering = {}  # Combinations of outlier filter results

        # Define relevant uncertainties, data ranges and max thresholds to be applied in Monte Carlo sampling
        self.uncertainty_meter = np.float64(uncertainty_meter)
        self.uncertainty_losses = np.float64(uncertainty_losses)
        self.uncertainty_windiness = np.array(uncertainty_windiness, dtype=np.float64)
        self.uncertainty_loss_max = np.array(uncertainty_loss_max, dtype=np.float64)
        self.uncertainty_max_power_filter = np.array(uncertainty_max_power_filter, dtype=np.float64)
        self.uncertainty_wind_bin_thresh = np.array(uncertainty_wind_bin_thresh, dtype=np.float64)
        self.uncertainty_nan_energy = np.float64(uncertainty_nan_energy)
        
        # Check that selected time resolution is allowed
        if time_resolution not in ['M','D']:
            raise ValueError("time_res has to either be M (monthly, default) or D (daily)")
        self.time_resolution = time_resolution
        if self.time_resolution == 'M':
            self.num_days_lt= (31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

        # Check that choices for regression inputs are allowed
        if reg_temperature not in ['Y', 'N']:
            raise ValueError("reg_temperature has to either be Y (if temperature is considered in the regression), or N (if temperature is omitted")
        if reg_winddirection not in ['Y', 'N']:
            raise ValueError("reg_winddirection has to either be Y (if wind direction is considered in the regression), or N (if wind direction is omitted")
        self.reg_winddirection = reg_winddirection
        self.reg_temperature = reg_temperature
        
        # Check that selected regression model is allowed
        if reg_model not in ['lin', 'gbm','etr','gam']:
            raise ValueError("reg_model has to either be lin (Linear regression, default), gbm (Gradient boosting model), etr (Extra trees regressor) or gam (Generalized additive model)")
        self.reg_model = reg_model
        
        # Monthly data can only use robus linear regression
        if (time_resolution == 'M') & (reg_model != 'lin'):
            raise ValueError("For monthly time resolution, only linear regression is allowed!")

        # Run preprocessing step                                                                                                                                                                              
        self.calculate_monthly_daily_dataframe()

        # Store start and end of period of record
        self._start_por = self._monthly_daily.df.index.min()
        self._end_por = self._monthly_daily.df.index.max()
        
        # Create a data frame to store monthly/daily reanalysis data over plant period of record
        self._reanalysis_por = self._monthly_daily.df.loc[(self._monthly_daily.df.index >= self._start_por) & \
                                                    (self._monthly_daily.df.index <= self._end_por)]
        if self.time_resolution == 'M':
            self._reanalysis_por_avg = self._reanalysis_por.groupby(self._reanalysis_por.index.month).mean()
            self._reanalysis_por_avg = self._reanalysis_por.groupby(self._reanalysis_por.index.month).mean()
        elif self.time_resolution == 'D':
            self._reanalysis_por_avg = self._reanalysis_por.groupby([(self._reanalysis_por.index.month),(self._reanalysis_por.index.day)]).mean()
            
    @logged_method_call
    def run(self, num_sim, reanal_subset):
        """
        Perform pre-processing of data into an internal representation for which the analysis can run more quickly.

        Args:
            reanal_subset(:obj:`list`): list of str data indicating which reanalysis products to use in OA
            num_sim(:obj:`int`): number of simulations to perform        
         
        :return: None
        """

        self.num_sim = num_sim
        self.reanal_subset = reanal_subset

        # Write parameters of run to the log file
        logged_self_params = ["uncertainty_meter", "uncertainty_losses", "uncertainty_loss_max", "uncertainty_windiness",
                              "uncertainty_nan_energy", "num_sim", "reanal_subset"]
        logged_params = {name: getattr(self, name) for name in logged_self_params}
        logger.info("Running with parameters: {}".format(logged_params))

        # Start the computation
        self.calculate_long_term_losses()
        self.setup_monte_carlo_inputs()
        self.results = self.run_AEP_monte_carlo()

        # Log the completion of the run
        logger.info("Run completed")

    def plot_reanalysis_normalized_rolling_monthly_windspeed(self):
        """
        Make a plot of annual average wind speeds from reanalysis data to show general trends for each
        Highlight the period of record for plant data

        :return: matplotlib.pyplot object
        """
        import matplotlib.pyplot as plt
        project = self._plant

        # Define parameters needed for plot
        min_val = 1  # Default parameter providing y-axis minimum for shaded plant POR region
        max_val = 1  # Default parameter providing y-axis maximum for shaded plant POR region
        por_start = self._monthly_daily.df.index[0]  # Start of plant POR
        por_end = self._monthly_daily.df.index[-1]  # End of plant POR

        plt.figure(figsize=(14, 6))
        for key, items in project._reanalysis._product.items():
            rean_df = project._reanalysis._product[key].df  # Set reanalysis product
            ann_mo_ws = rean_df.resample('MS')['ws_dens_corr'].mean().to_frame()  # Take monthly average wind speed
            ann_roll = ann_mo_ws.rolling(12).mean()  # Calculate rolling 12-month average
            ann_roll_norm = ann_roll['ws_dens_corr'] / ann_roll[
                'ws_dens_corr'].mean()  # Normalize rolling 12-month average

            # Update min_val and max_val depending on range of data
            if ann_roll_norm.min() < min_val:
                min_val = ann_roll_norm.min()
            if ann_roll_norm.max() > max_val:
                max_val = ann_roll_norm.max()

            # Plot wind speed
            plt.plot(ann_roll_norm, label=key)

        # Plot dotted line at y=1 (i.e. average wind speed)
        plt.plot((ann_roll.index[0], ann_roll.index[-1]), (1, 1), 'k--')

        # Fill in plant POR region
        plt.fill_between([por_start, por_end], [min_val, min_val], [max_val, max_val], alpha=0.1, label='Plant POR')

        # Final touches to plot
        plt.xlabel('Year')
        plt.ylabel('Normalized wind speed')
        plt.legend()
        plt.tight_layout()
        return plt

    def plot_reanalysis_gross_energy_data(self, outlier_thres):
        """
        Make a plot of normalized 30-day gross energy vs wind speed for each reanalysis product, include R2 measure

        :param outlier_thres (float): outlier threshold (typical range of 1 to 4) which adjusts outlier sensitivity
        detection

        :return: matplotlib.pyplot object
        """
        import matplotlib.pyplot as plt
        valid_monthly_daily = self._monthly_daily.df
        project = self._plant
        plt.figure(figsize=(9, 9))

        # Loop through each reanalysis product and make a scatterplot of monthly wind speed vs plant energy
        for p in np.arange(0, len(list(project._reanalysis._product.keys()))):
            col_name = list(project._reanalysis._product.keys())[p]  # Reanalysis column in monthly data frame

            x = sm.add_constant(valid_monthly_daily[col_name])  # Define 'x'-values (constant needed for regression function)
            if self.time_resolution == 'M':
                y = valid_monthly_daily['gross_energy_gwh'] * 30 / valid_monthly_daily[
                    'num_days_expected']  # Normalize energy data to 30-days
            elif self.time_resolution == 'D':
                y = valid_monthly_daily['gross_energy_gwh']
                
            rlm = sm.RLM(y, x, M=sm.robust.norms.HuberT(
                t=outlier_thres))  # Robust linear regression with HuberT algorithm (threshold equal to 2)
            rlm_results = rlm.fit()

            r2 = np.corrcoef(x.loc[rlm_results.weights == 1, col_name], y[rlm_results.weights == 1])[
                0, 1]  # Get R2 from valid data

            # Plot results
            plt.subplot(2, 2, p + 1)
            plt.plot(x.loc[rlm_results.weights != 1, col_name], y[rlm_results.weights != 1], 'rx', label='Outlier')
            plt.plot(x.loc[rlm_results.weights == 1, col_name], y[rlm_results.weights == 1], '.', label='Valid data')
            plt.title(col_name + ', R2=' + str(np.round(r2, 3)))
            plt.xlabel('Wind speed (m/s)')
            if self.time_resolution == 'M':
                plt.ylabel('30-day normalized gross energy (GWh)')
            elif self.time_resolution == 'D':
                plt.ylabel('Daily gross energy (GWh)')
        plt.tight_layout()
        return plt

    def plot_result_aep_distributions(self):
        """
        Plot a distribution of APE values from the Monte-Carlo OA method

        :return: matplotlib.pyplot object
        """
        import matplotlib.pyplot as plt
        fig = plt.figure(figsize=(14, 12))

        sim_results = self.results

        ax = fig.add_subplot(2, 2, 1)
        ax.hist(sim_results['aep_GWh'], 40, normed=1)
        ax.text(0.05, 0.9, 'AEP mean = ' + str(np.round(sim_results['aep_GWh'].mean(), 1)) + ' GWh/yr',
                transform=ax.transAxes)
        ax.text(0.05, 0.8, 'AEP unc = ' + str(
            np.round(sim_results['aep_GWh'].std() / sim_results['aep_GWh'].mean() * 100, 1)) + "%",
                transform=ax.transAxes)
        plt.xlabel('AEP (GWh/yr)')

        ax = fig.add_subplot(2, 2, 2)
        ax.hist(sim_results['avail_pct'] * 100, 40, normed=1)
        ax.text(0.05, 0.9, 'Mean = ' + str(np.round((sim_results['avail_pct'].mean()) * 100, 1)) + ' %',
                transform=ax.transAxes)
        plt.xlabel('Availability Loss (%)')

        ax = fig.add_subplot(2, 2, 3)
        ax.hist(sim_results['curt_pct'] * 100, 40, normed=1)
        ax.text(0.05, 0.9, 'Mean: ' + str(np.round((sim_results['curt_pct'].mean()) * 100, 2)) + ' %',
                transform=ax.transAxes)
        plt.xlabel('Curtailment Loss (%)')
        plt.tight_layout()
        return plt

    def plot_aep_boxplot(self, param, lab):
        """                                                                                                                                                                                        
        Plot box plots of AEP results sliced by a specified Monte Carlo parameter                                                                                                                  

        Args:                                                                                                                                                                                      
           param(:obj:`list'): The Monte Carlo parameter on which to split the AEP results
           lab(:obj:'str'): The name to use for the parameter when producing the figure
        
        Returns:                                                                                                                                                                                   
            (none)                                                                                                                                                                               
        """

        import matplotlib.pyplot as plt
        sim_results = self.results

        tmp_df=pd.DataFrame(data={'aep': sim_results.aep_GWh, 'param': param})
        tmp_df.boxplot(column='aep',by='param',figsize=(8,6))
        plt.ylabel('AEP (GWh/yr)')
        plt.xlabel(lab)
        plt.title('AEP estimates by %s' % lab)
        plt.suptitle("")
        plt.tight_layout()
        return plt

    def plot_monthly_daily_plant_data_timeseries(self):
        """
        Plot timeseries of monthly/daily gross energy, availability and curtailment

        :return: matplotlib.pyplot object
        """
        import matplotlib.pyplot as plt
        valid_monthly_daily = self._monthly_daily.df

        plt.figure(figsize=(12, 9))

        # Gross energy
        plt.subplot(2, 1, 1)
        plt.plot(valid_monthly_daily.gross_energy_gwh, '.-')
        plt.grid('on')
        plt.xlabel('Year')
        plt.ylabel('Gross energy (GWh)')

        # Availability and curtailment
        plt.subplot(2, 1, 2)
        plt.plot(valid_monthly_daily.availability_pct * 100, '.-', label='Availability')
        plt.plot(valid_monthly_daily.curtailment_pct * 100, '.-', label='Curtailment')
        plt.grid('on')
        plt.xlabel('Year')
        plt.ylabel('Loss (%)')
        plt.legend()
        
        plt.tight_layout()
        return plt

    @logged_method_call
    def calculate_monthly_daily_dataframe(self):
        """
        Perform pre-processing of the plant data to produce a monthly/daily data frame to be used in AEP analysis.
        Args:
            (None)

        Returns:
            (None)
        """

        # Average to monthly/daily, quantify NaN data
        self.process_revenue_meter_energy()

        # Average to monthly/daily, quantify NaN data, merge with revenue meter energy data
        self.process_loss_estimates()

        # Density correct wind speeds, process temperature and wind direction, average to monthly/daily
        self.process_reanalysis_data()

        # Remove first and last reporting months if only partial month reported
        # (only for monthly time resolution calculations)
        if self.time_resolution == 'M':
           self.trim_monthly_df()

        # Drop any data that have NaN gross energy values or NaN reanalysis data
        self._monthly_daily.df = self._monthly_daily.df.loc[np.isfinite(self._monthly_daily.df.gross_energy_gwh) & 
                                                np.isfinite(self._monthly_daily.df.ncep2) & 
                                                np.isfinite(self._monthly_daily.df.merra2) & 
                                                np.isfinite(self._monthly_daily.df.erai)]

    @logged_method_call
    def process_revenue_meter_energy(self):
        """
        Initial creation of monthly data frame:
            1. Populate monthly/daily data frame with energy data summed from 10-min QC'd data
            2. For each monthly/daily value, find percentage of NaN data used in creating it and flag if percentage is
            greater than 0

        Args:
            (None)

        Returns:
            (None)

        """
        df = getattr(self._plant, 'meter').df  # Get the meter data frame

        if self.time_resolution == 'M':
            
            # Create the monthly data frame by summing meter energy into yr-mo
            self._monthly_daily.df = (df.resample('MS')['energy_kwh'].sum() / 1e6).to_frame()  # Get monthly energy values in GWh
            self._monthly_daily.df.rename(columns={"energy_kwh": "energy_gwh"}, inplace=True)  # Rename kWh to MWh
    
            # Determine how much 10-min data was missing for each year-month energy value. Flag accordigly if any is missing
            self._monthly_daily.df['energy_nan_perc'] = df.resample('MS')['energy_kwh'].apply(
                tm.percent_nan)  # Get percentage of meter data that were NaN when summing to monthly
    
            # Create a column with expected number of days per month (to be used when normalizing to 30-days for regression)
            days_per_month = (pd.Series(self._monthly_daily.df.index)).dt.daysinmonth
            days_per_month.index = self._monthly_daily.df.index
            self._monthly_daily.df['num_days_expected'] = days_per_month
    
            # Get actual number of days per month in the raw data
            # (used when trimming beginning and end of monthly data frame)
            # If meter data has higher resolution than monthly
            if (self._plant._meter_freq == '1MS') | (self._plant._meter_freq == '1M'):
                self._monthly_daily.df['num_days_actual'] = self._monthly_daily.df['num_days_expected']            
            else:
                self._monthly_daily.df['num_days_actual'] = df.resample('MS')['energy_kwh'].apply(tm.num_days)
                
        elif self.time_resolution == 'D':

            # Create the daily data frame by summing meter energy into yr-mo
            self._monthly_daily.df = (df.resample('D')['energy_kwh'].sum() / 1e6).to_frame()  # Get daily energy values in GWh
            self._monthly_daily.df.rename(columns={"energy_kwh": "energy_gwh"}, inplace=True)  # Rename kWh to MWh
    
            # Determine how much 10-min data was missing for each daily energy value. Flag accordigly if any is missing
            self._monthly_daily.df['energy_nan_perc'] = df.resample('D')['energy_kwh'].apply(
                tm.percent_nan)  # Get percentage of meter data that were NaN when summing to daily
            # No need to calculate firther has no normalization will be needed for daily data

    @logged_method_call
    def process_loss_estimates(self):
        """
        Append availability and curtailment losses to monthly data frame

        Args:
            (None)

        Returns:
            (None)

        """
        df = getattr(self._plant, 'curtail').df

        if self.time_resolution == 'M':
            curt_monthly_daily = np.divide(df.resample('MS')[['availability_kwh', 'curtailment_kwh']].sum(),
                                 1e6)  # Get sum of avail and curt losses in GWh
                
        elif self.time_resolution == 'D':
              curt_monthly_daily = np.divide(df.resample('D')[['availability_kwh', 'curtailment_kwh']].sum(),
                                 1e6)  # Get sum of avail and curt losses in GWh
              
        curt_monthly_daily.rename(columns={'availability_kwh': 'availability_gwh', 'curtailment_kwh': 'curtailment_gwh'},
                            inplace=True)  
        # Merge with revenue meter monthly/daily data
        self._monthly_daily.df = self._monthly_daily.df.join(curt_monthly_daily)

        # Add gross energy field
        self._monthly_daily.df['gross_energy_gwh'] = un.compute_gross_energy(self._monthly_daily.df['energy_gwh'],
                                                                       self._monthly_daily.df['availability_gwh'],
                                                                       self._monthly_daily.df['curtailment_gwh'], 'energy',
                                                                       'energy')

        # Calculate percentage-based losses
        self._monthly_daily.df['availability_pct'] = np.divide(self._monthly_daily.df['availability_gwh'],
                                                         self._monthly_daily.df['gross_energy_gwh'])
        self._monthly_daily.df['curtailment_pct'] = np.divide(self._monthly_daily.df['curtailment_gwh'],
                                                        self._monthly_daily.df['gross_energy_gwh'])

        if self.time_resolution == 'M':
            self._monthly_daily.df['avail_nan_perc'] = df.resample('MS')['availability_kwh'].apply(
                tm.percent_nan)  # Get percentage of 10-min meter data that were NaN when summing to monthly
            self._monthly_daily.df['curt_nan_perc'] = df.resample('MS')['curtailment_kwh'].apply(
                tm.percent_nan)  # Get percentage of 10-min meter data that were NaN when summing to monthly

        elif self.time_resolution == 'D':
            self._monthly_daily.df['avail_nan_perc'] = df.resample('D')['availability_kwh'].apply(
                tm.percent_nan)  # Get percentage of 10-min meter data that were NaN when summing to daily
            self._monthly_daily.df['curt_nan_perc'] = df.resample('D')['curtailment_kwh'].apply(
                tm.percent_nan)  # Get percentage of 10-min meter data that were NaN when summing to daily
        
        self._monthly_daily.df['nan_flag'] = False  # Set flag to false by default
        self._monthly_daily.df.loc[(self._monthly_daily.df['energy_nan_perc'] > self.uncertainty_nan_energy) |
                             (self._monthly_daily.df['avail_nan_perc'] > self.uncertainty_nan_energy) |
                             (self._monthly_daily.df['curt_nan_perc'] > self.uncertainty_nan_energy), 'nan_flag'] \
            = True  # If more than 1% of data are NaN, set flag to True

        # By default, assume all reported losses are representative of long-term operational
        self._monthly_daily.df['availability_typical'] = True
        self._monthly_daily.df['curtailment_typical'] = True

        # By default, assume combined availability and curtailment losses are below the threshold to be considered valid
        self._monthly_daily.df['combined_loss_valid'] = True
        
    @logged_method_call
    def process_reanalysis_data(self):
        """
        Process reanalysis data for use in PRUF plant analysis
            - calculate density-corrected wind speed and wind components
            - get monthly/daily average wind speeds and components
            - calculate monthly/daily average wind direction
            - calculate monthly/daily average temperature
            - append monthly/daily averages to monthly/daily energy data frame

        Args:
            (None)

        Returns:
            (None)
        """
        
        if self.time_resolution == 'M':
            
            # Define empty data frame that spans past our period of interest
            self._reanalysis_monthly_daily = pd.DataFrame(index=pd.date_range(start='1997-01-01', end='2020-01-01',
                                                                        freq='MS'), dtype=float)
    
            # Now loop through the different reanalysis products, density-correct wind speeds, and take monthly averages
            for key, items in self._plant._reanalysis._product.items():
                rean_df = self._plant._reanalysis._product[key].df
                rean_df['ws_dens_corr'] = mt.air_density_adjusted_wind_speed(rean_df, 'windspeed_ms',
                                                                             'rho_kgm-3')  # Density correct wind speeds
                self._reanalysis_monthly_daily[key] = rean_df.resample('MS')['ws_dens_corr'].mean()  # .to_frame() # Get average wind speed by year-month
    
                if self.reg_temperature == 'Y': # if temperature is considered as regression variable
                    self._reanalysis_monthly_daily[key + '_temp'] = pd.to_numeric(rean_df['temperature_K']).resample('MS').mean()  # .to_frame() # Get average temperature by year-month
                
                if self.reg_winddirection == 'Y': # if wind direction is considered as regression variable
                    u_avg = pd.to_numeric(rean_df['u_ms']).resample('MS').mean()  # .to_frame() # Get average u component by year-month
                    v_avg = pd.to_numeric(rean_df['v_ms']).resample('MS').mean()  # .to_frame() # Get average v component by year-month
                    self._reanalysis_monthly_daily[key + '_u'] = u_avg 
                    self._reanalysis_monthly_daily[key + '_v'] = v_avg
                    self._reanalysis_monthly_daily[key + '_wd'] = 180-np.rad2deg(np.arctan2(-u_avg,v_avg)) # Calculate wind direction
                
            self._monthly_daily.df = self._monthly_daily.df.join(
                self._reanalysis_monthly_daily)  # Merge monthly reanalysis data to monthly energy data frame
                 
        elif self.time_resolution == 'D':

            # Define empty data frame that spans past our period of interest
            self._reanalysis_monthly_daily = pd.DataFrame(index=pd.date_range(start='1997-01-01', end='2020-01-01',
                                                                        freq='D'), dtype=float)
    
            # Now loop through the different reanalysis products, density-correct wind speeds, and take daily averages
            for key, items in self._plant._reanalysis._product.items():
                rean_df = self._plant._reanalysis._product[key].df
                rean_df['ws_dens_corr'] = mt.air_density_adjusted_wind_speed(rean_df, 'windspeed_ms',
                                                                             'rho_kgm-3')  # Density correct wind speeds
                self._reanalysis_monthly_daily[key] = rean_df.resample('D')['ws_dens_corr'].mean()  # .to_frame() # Get average wind speed by day
                
                if self.reg_temperature == 'Y': # if temperature is considered as regression variable
                    self._reanalysis_monthly_daily[key + '_temp'] = pd.to_numeric(rean_df['temperature_K']).resample('D').mean() #.to_frame() # Get average temperature by day
                
                if self.reg_winddirection == 'Y': # if wind direction is considered as regression variable
                    u_avg = pd.to_numeric(rean_df['u_ms']).resample('D').mean()  # .to_frame() # Get average u component by day
                    v_avg = pd.to_numeric(rean_df['v_ms']).resample('D').mean()  # .to_frame() # Get average v component by day
                    self._reanalysis_monthly_daily[key + '_u'] = u_avg 
                    self._reanalysis_monthly_daily[key + '_v'] = v_avg
                    self._reanalysis_monthly_daily[key + '_wd'] = 180-np.rad2deg(np.arctan2(-u_avg,v_avg)) # Calculate wind direction
                
            self._monthly_daily.df = self._monthly_daily.df.join(
                self._reanalysis_monthly_daily)  # Merge daily reanalysis data to daily energy data frame
                             
    @logged_method_call
    def trim_monthly_df(self):
        """
        Remove first and/or last month of data if the raw data had an incomplete number of days

        Args:
            (None)

        Returns:
            (None)
        """
        for p in self._monthly_daily.df.index[[0, -1]]:  # Loop through 1st and last data entry
            if self._monthly_daily.df.loc[p, 'num_days_expected'] != self._monthly_daily.df.loc[p, 'num_days_actual']:
                self._monthly_daily.df.drop(p, inplace=True)  # Drop the row from data frame

    @logged_method_call
    def calculate_long_term_losses(self):
        """
        This function calculates long-term availability and curtailment losses based on the reported data,
        filtering for those data that are deemed representative of average plant performance

        Args:
            (None)

        Returns:
            (tuple):
                :obj:`float`: long-term annual availability loss expressed as fraction
                :obj:`float`: long-term annual curtailment loss expressed as fraction
        """
        df = self._monthly_daily.df
        
        days_year_lt = 365.25 # Number of days per long-term year (accounting for leap year every 4 years)

        # isolate availabilty and curtailment values that are representative of average plant performance
        avail_valid = df.loc[df['availability_typical'],'availability_pct'].to_frame()
        curt_valid = df.loc[df['curtailment_typical'],'curtailment_pct'].to_frame()
            
        if self.time_resolution == 'M':
        
            # Now get average percentage losses by month
            avail_long_term=avail_valid.groupby(avail_valid.index.month)['availability_pct'].mean()
            curt_long_term=curt_valid.groupby(curt_valid.index.month)['curtailment_pct'].mean()
    
            # Ensure there are 12 data points in long-term average. If not, throw an exception:
            if (avail_long_term.shape[0] != 12):
                raise Exception('Not all calendar months represented in long-term availability calculation')
                 
            if (curt_long_term.shape[0] != 12):
                raise Exception('Not all calendar months represented in long-term curtailment calculation')
           
            # Merge long-term losses and number of long-term days
            lt_losses_df = pd.DataFrame(data = {'avail': avail_long_term, 'curt': curt_long_term, 'n_days': self.num_days_lt})
            
            # Calculate long-term annual availbilty and curtailment losses, weighted by number of days per month
            lt_losses_df['avail_weighted'] = lt_losses_df['avail'].multiply(lt_losses_df['n_days'])
            lt_losses_df['curt_weighted'] = lt_losses_df['curt'].multiply(lt_losses_df['n_days'])
            avail_annual = lt_losses_df['avail_weighted'].sum()/days_year_lt
            curt_annual = lt_losses_df['curt_weighted'].sum()/days_year_lt
            
            # Assign long-term annual losses to plant analysis object
            self.long_term_losses = (avail_annual, curt_annual)

        elif self.time_resolution == 'D':

            # Now get average percentage losses by day
            avail_long_term=avail_valid.groupby([(avail_valid.index.month),(avail_valid.index.day)])['availability_pct'].mean()
            curt_long_term=curt_valid.groupby([(curt_valid.index.month),(curt_valid.index.day)])['curtailment_pct'].mean()
    
            # Ensure there are 366 data points in long-term average. If not, throw an exception:
            if (avail_long_term.shape[0] != 366):
                raise Exception('Not all calendar days represented in long-term availability calculation')
                 
            if (curt_long_term.shape[0] != 366):
                raise Exception('Not all calendar days represented in long-term curtailment calculation')
           
            # Merge long-term losses
            lt_losses_df = pd.DataFrame(data = {'avail': avail_long_term, 'curt': curt_long_term})
            
            # Calculate long-term annual availbilty and curtailment losses
            avail_annual = lt_losses_df['avail'].sum()/days_year_lt
            curt_annual = lt_losses_df['curt'].sum()/days_year_lt
            
            # Assign long-term annual losses to plant analysis object
            self.long_term_losses = (avail_annual, curt_annual)

    @logged_method_call
    def setup_monte_carlo_inputs(self):
        """
        Perform Monte Carlo sampling for reported monthly/daily revenue meter energy, availability, and curtailment data,
        as well as reanalysis data

        Args:
            (None)

        Returns:
            (None)
        """
        
        reanal_subset = self.reanal_subset
        
        num_sim = self.num_sim

        if (self.reg_winddirection == 'Y') & (self.reg_temperature == 'Y'):
            self._mc_slope = np.empty([num_sim,4], dtype=np.float64)
        elif (self.reg_winddirection == 'Y') & (self.reg_temperature == 'N'):
            self._mc_slope = np.empty([num_sim,3], dtype=np.float64)
        elif (self.reg_winddirection == 'N') & (self.reg_temperature == 'Y'):
            self._mc_slope = np.empty([num_sim,2], dtype=np.float64)
        else:
            self._mc_slope = np.empty(num_sim, dtype=np.float64)
        self._mc_intercept = np.empty(num_sim, dtype=np.float64)
        self._mc_num_points = np.empty(num_sim, dtype=np.float64)
        self._r2_score = np.empty(num_sim, dtype=np.float64)
        self._mse_score = np.empty(num_sim, dtype=np.float64)       
        
        self._mc_max_power_filter = np.random.randint(self.uncertainty_max_power_filter[0]*100, self.uncertainty_max_power_filter[1]*100,
                                                        self.num_sim) / 100.  
        self._mc_wind_bin_thresh = np.random.randint(self.uncertainty_wind_bin_thresh[0]*100, self.uncertainty_wind_bin_thresh[1]*100,
                                                        self.num_sim) / 100.  
        self._mc_metered_energy_fraction = np.random.normal(1, self.uncertainty_meter, num_sim)
        self._mc_loss_fraction = np.random.normal(1, self.uncertainty_losses, num_sim)
        self._mc_num_years_windiness = np.random.randint(self.uncertainty_windiness[0],
                                                         self.uncertainty_windiness[1] + 1, num_sim)
        self._mc_loss_threshold = np.random.randint(self.uncertainty_loss_max[0], self.uncertainty_loss_max[1] + 1,
                                                    num_sim) / 100.

        reanal_list = list(np.repeat(reanal_subset,num_sim))  # Create extra long list of renanalysis product names to sample from
        self._mc_reanalysis_product = np.asarray(random.sample(reanal_list, num_sim))


    @logged_method_call
    def filter_outliers(self, n):
        """
        This function filters outliers based on a combination of range filter, unresponsive sensor filter, 
        and window filter.

        We use a memoized funciton to store the regression data in a dictionary for each combination as it
        comes up in the Monte Carlo simulation. This saves significant computational time in not having to run
        robust linear regression for each Monte Carlo iteration

        Args:
            n(:obj:`float`): Monte Carlo iteration

        Returns:
            :obj:`pandas.DataFrame`: Filtered monthly/daily data ready for linear regression
        """
        
        reanal = self._mc_reanalysis_product[n]
        max_power_filter = self._mc_max_power_filter[n]
        comb_loss_thresh = self._mc_loss_threshold[n]
        
        # Check if valid data has already been calculated and stored. If so, just return it
        if (reanal, max_power_filter, comb_loss_thresh) in self.outlier_filtering:
            valid_data = self.outlier_filtering[(reanal, max_power_filter, comb_loss_thresh)]
            return valid_data

        # If valid data hasn't yet been stored in dictionary, determine the valid data
        df = self._monthly_daily.df
                
        # First set of filters checking combined losses and if the Nan data flag was on
        df_sub = df.loc[
            ((df['availability_pct'] + df['curtailment_pct']) < comb_loss_thresh) & (df['nan_flag'] == False),:]
                
        # Set maximum range for using bin-filter, convert from MW to GWh
        if self.time_resolution == 'M':
            plant_capac = getattr(self._plant, '_plant_capacity')/1000 * 366*24
        elif self.time_resolution == 'D':
            plant_capac = getattr(self._plant, '_plant_capacity')/1000 * 1*24
        
        # Flag turbine energy data less than zero
        df_sub.loc[:,'flag_neg'] = filters.range_flag(df_sub['energy_gwh'], below = 0, above = plant_capac)
        # Apply range filter to wind speed
        df_sub.loc[:,'flag_range'] = filters.range_flag(df_sub[reanal], below = 0, above = 40)
        # Apply frozen/unresponsive sensor filter
        df_sub.loc[:,'flag_frozen'] = filters.unresponsive_flag(df_sub[reanal], threshold = 3)
        # Apply window range filter
        df_sub.loc[:,'flag_window'] = filters.window_range_flag(window_col = df_sub[reanal], 
                                                                    window_start = 5., 
                                                                    window_end = 40,
                                                                    value_col = df_sub['energy_gwh'], 
                                                                    value_min =  0.02*plant_capac,
                                                                    value_max =  1.2*plant_capac) 
        
        # Create a 'final' flag which is true if any of the previous flags are true
        df_sub.loc[:,'flag_final'] = (df_sub.loc[:, 'flag_range']) | (df_sub.loc[:, 'flag_frozen']) #| \
                                          #(df_sub.loc[:, 'flag_window']) 
        
        # Set negative turbine data to zero
        df_sub.loc[df_sub['flag_neg'], 'energy_gwh'] = 0
                
        # Define valid data as points in which the Huber algorithm returned a value of 1
        if self.time_resolution == 'M':
            if (self.reg_winddirection == 'Y') & (self.reg_temperature == 'Y'):
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal, reanal + '_wd', reanal + '_temp',
                                                               reanal + '_u', reanal + '_v',
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh', 'num_days_expected']]
            elif (self.reg_winddirection == 'Y') & (self.reg_temperature == 'N'):
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal, reanal + '_wd',
                                                               reanal + '_u', reanal + '_v',
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh', 'num_days_expected']]
            elif (self.reg_winddirection == 'N') & (self.reg_temperature == 'Y'):
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal, reanal + '_temp',
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh', 'num_days_expected']]
            else:
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal,
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh', 'num_days_expected']]
        elif self.time_resolution == 'D': 
            if (self.reg_winddirection == 'Y') & (self.reg_temperature == 'Y'):
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal, reanal + '_wd', reanal + '_temp',
                                                               reanal + '_u', reanal + '_v',
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh']]
            elif (self.reg_winddirection == 'Y') & (self.reg_temperature == 'N'):
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal, reanal + '_wd',
                                                               reanal + '_u', reanal + '_v',
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh']]
            elif (self.reg_winddirection == 'N') & (self.reg_temperature == 'Y'):
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal, reanal + '_temp',
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh']]
            else:
                valid_data = df_sub.loc[df_sub.loc[:, 'flag_final'] == False, [reanal,
                                                               'energy_gwh', 'availability_gwh',
                                                               'curtailment_gwh']]
              
        # Update the dictionary
        self.outlier_filtering[(reanal, max_power_filter, comb_loss_thresh)] = valid_data

        # Return result
        return valid_data

    @logged_method_call
    def set_regression_data(self, n):
        """
        This will be called for each iteration of the Monte Carlo simulation and will do the following:
            1. Randomly sample monthly/daily revenue meter, availabilty, and curtailment data based on specified uncertainties
            and correlations
            2. Randomly choose one reanalysis product
            3. Calculate gross energy from randomzied energy data
            4. Normalize gross energy to 30-day months
            5. Filter results to remove months/days with NaN data and with combined losses that exceed the Monte Carlo
            sampled max threhold
            6. Return the wind speed and normalized gross energy to be used in the regression relationship

        Args:
            n(:obj:`int`): The Monte Carlo iteration number

        Returns:
            :obj:`pandas.Series`: Monte-Carlo sampled wind speeds and other variables (temperature, wind direction) if used in the regression
            :obj:`pandas.Series`: Monte-Carlo sampled normalized gross energy

        """
        # Get data to use in regression based on filtering result
        reg_data = self.filter_outliers(n)

        # Now monte carlo sample the data
        mc_energy = reg_data['energy_gwh'] * self._mc_metered_energy_fraction[
            n]  # Create new Monte-Carlo sampled data frame and sample energy data
        mc_availability = reg_data['availability_gwh'] * self._mc_loss_fraction[
            n]  # Calculate MC-generated availability
        mc_curtailment = reg_data['curtailment_gwh'] * self._mc_loss_fraction[n]  # Calculate MC-generated curtailment

        # Calculate gorss energy and normalize to 30-days
        mc_gross_energy = mc_energy + mc_availability + mc_curtailment
        if self.time_resolution == 'M':
            num_days_expected = reg_data['num_days_expected']
            mc_gross_norm = mc_gross_energy * 30 / num_days_expected  # Normalize gross energy to 30-day months
        elif self.time_resolution == 'D':   
            mc_gross_norm = mc_gross_energy
            
        # Set reanalysis product
        reg_inputs = reg_data[self._mc_reanalysis_product[n]]  # Copy wind speed data to Monte Carlo data frame
        
        if self.reg_temperature == 'Y': # if temperature is considered as regression variable
            mc_temperature = reg_data[self._mc_reanalysis_product[n] + "_temp"]  # Copy temperature data to Monte Carlo data frame
            reg_inputs = pd.concat([reg_inputs,mc_temperature], axis = 1)
            
        if self.reg_winddirection == 'Y': # if wind direction is considered as regression variable
            mc_wind_direction = reg_data[self._mc_reanalysis_product[n] + "_wd"]  # Copy wind direction data to Monte Carlo data frame
            mc_wind_direction_sin = np.sin(2*np.pi*mc_wind_direction/360)
            mc_wind_direction_cos = np.cos(2*np.pi*mc_wind_direction/360)
            reg_inputs = pd.concat([reg_inputs,mc_wind_direction_sin], axis = 1)
            reg_inputs = pd.concat([reg_inputs,mc_wind_direction_cos], axis = 1)
   
        reg_inputs = pd.concat([reg_inputs,mc_gross_norm], axis = 1)
        # Return values needed for regression
        return reg_inputs  # Return randomly sampled wind speed, wind direction, temperature and normalized gross energy

    @logged_method_call
    def run_regression(self, n):
        """
        Run robust linear regression between Monte-Carlo generated monthly/daily gross energy, 
        wind speed, temperature and wind direction (if used)
        Return Monte-Carlo sampled slope and intercept values (based on their covariance) and report
        the number of outliers based on the robust linear regression result.

        Args:
            n(:obj:`int`): The Monte Carlo iteration number

        Returns:
            :obj:`float`: Monte-carlo sampled slope
            :obj:`float`: Monte-carlo sampled intercept
        """
        reg_data = self.set_regression_data(n)  # Get regression data
        
        # Randomly select 80% of the data to perform regression and incorporate some regression uncertainty
        reg_data = np.array(reg_data.sample(frac = 0.8))
        
        # Update Monte Carlo tracker fields
        self._mc_num_points[n] = np.shape(reg_data)[0]
        
        # Linear regression
        if self.reg_model == 'lin':
            if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                reg = LinearRegression().fit(np.array(reg_data[:,0].reshape(-1,1)), reg_data[:,-1])
                predicted_y = reg.predict(np.array(reg_data[:,0].reshape(-1,1)))
            else:
                reg = LinearRegression().fit(np.array(reg_data[:,0:-1]), reg_data[:,-1])
                predicted_y = reg.predict(np.array(reg_data[:,0:-1]))
            mc_slope = reg.coef_
            mc_intercept = reg.intercept_  
            if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                self._mc_slope[n] = (mc_slope)
            else:
                self._mc_slope[n,:] = (mc_slope)
            self._mc_intercept[n] = np.float(mc_intercept)
            
            self._r2_score[n] = r2_score(reg_data[:,-1], predicted_y)
            self._mse_score[n] = mean_squared_error(reg_data[:,-1], predicted_y)
            return mc_slope, mc_intercept
        # Machine learning models
        else: 
            ml = MachineLearningSetup(self.reg_model)
            if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                ml.hyper_optimize(np.array(reg_data[:,0].reshape(-1,1)), reg_data[:,-1], n_iter_search = 5, report = False, cv = KFold(n_splits = 2))
                predicted_y = ml.random_search.predict(np.array(reg_data[:,0].reshape(-1,1)))
            else:
                ml.hyper_optimize(np.array(reg_data[:,0:-1]), reg_data[:,-1], n_iter_search = 5, report = False, cv = KFold(n_splits = 2))
                predicted_y = ml.random_search.predict(np.array(reg_data[:,0:-1]))
                 
            self._r2_score[n] = r2_score(reg_data[:,-1], predicted_y)
            self._mse_score[n] = mean_squared_error(reg_data[:,-1], predicted_y)
            return ml.random_search


    @logged_method_call
    def run_AEP_monte_carlo(self):
        """
        Loop through OA process a number of times and return array of AEP results each time

        Returns:
            :obj:`numpy.ndarray` Array of AEP, long-term avail, long-term curtailment calculations
        """

        num_sim = self.num_sim

        aep_GWh = np.empty(num_sim)
        avail_pct =  np.empty(num_sim)
        curt_pct =  np.empty(num_sim)
        lt_por_ratio =  np.empty(num_sim)

        # Linear regression
        if self.reg_model == 'lin':
            # Loop through number of simulations, run regression each time, store AEP results
            for n in tqdm(np.arange(num_sim)):
                slope, intercept = self.run_regression(n)  # Get slope, intercept from regression
                reg_inputs_lt = self.sample_long_term_reanalysis(self._mc_num_years_windiness[n],
                                                             self._mc_reanalysis_product[n])  # Get long-term wind speeds
                    
                # Get long-term normalized gross energy by applying regression result to long-term monthly wind speeds
                if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                    gross_norm_lt = reg_inputs_lt.multiply(slope[0]) + intercept
                else:
                    gross_norm_lt = reg_inputs_lt.multiply(slope.reshape(-1,1).T, axis='columns').sum(axis='columns') + intercept
                if self.time_resolution == 'M':
                    gross_lt = gross_norm_lt*self.num_days_lt/30 # Undo normalization to 30-day months
                else:
                    gross_lt = gross_norm_lt
                        
                # Get POR gross energy by applying regression result to POR regression inputs                                                                    
                if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                    gross_norm_por = self._reanalysis_por_avg[self._mc_reanalysis_product[n]].multiply(slope[0]) + intercept
                elif (self.reg_temperature == 'Y') & (self.reg_winddirection == 'N'):
                    gross_norm_por = self._reanalysis_por_avg[self._mc_reanalysis_product[n]].multiply(slope[0]) + \
                                    self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_temp'].multiply(slope[1]) + intercept
                elif (self.reg_temperature == 'N') & (self.reg_winddirection == 'Y'):
                    wd_sin = np.sin(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    wd_cos = np.cos(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    gross_norm_por = self._reanalysis_por_avg[self._mc_reanalysis_product[n]].multiply(slope[0]) + \
                                    wd_sin * slope[1] + wd_cos * slope[2] + intercept
                else:
                    wd_sin = np.sin(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    wd_cos = np.cos(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    gross_norm_por = self._reanalysis_por_avg[self._mc_reanalysis_product[n]].multiply(slope[0]) + \
                                    self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_temp'].multiply(slope[1]) + \
                                    wd_sin * slope[2] + wd_cos * slope[3] + intercept
                if self.time_resolution == 'M':
                    gross_por=gross_norm_por*self.num_days_lt/30 # Undo normalization to 30-day months 
                else:
                    gross_por=gross_norm_por
                        
                # Get long-term availability and curtailment losses by month
                [avail_lt_losses, curt_lt_losses] = self.sample_long_term_losses(n)  
        
                # Assign AEP, long-term availability, and long-term curtailment to output data frame
                aep_GWh[n] = gross_lt.sum() * (1 - avail_lt_losses)
                avail_pct[n] = avail_lt_losses
                curt_pct[n] = curt_lt_losses
                lt_por_ratio[n] = gross_lt.sum() / gross_por.sum()                    
         
        # Machine learning models
        else: 
            # Loop through number of simulations, run regression each time, store AEP results
            for n in tqdm(np.arange(num_sim)):
                reg_inputs_lt = self.sample_long_term_reanalysis(self._mc_num_years_windiness[n],
                                                             self._mc_reanalysis_product[n])  # Get long-term regression inputs
                    
                ml_model_cv = self.run_regression(n)

                # Get long-term normalized gross energy by applying regression result to long-term monthly wind speeds
                if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                    gross_lt = ml_model_cv.predict(np.array(reg_inputs_lt).reshape(-1, 1))
                else:
                    gross_lt = ml_model_cv.predict(np.array(reg_inputs_lt))
                    
                # Get POR gross energy by applying regression result to POR regression inputs                                                                        
                if (self.reg_temperature == 'N') & (self.reg_winddirection == 'N'):
                    reg_inputs_por = self._reanalysis_por_avg[self._mc_reanalysis_product[n]]
                    gross_por = ml_model_cv.predict(np.array(reg_inputs_por).reshape(-1, 1))
                elif (self.reg_temperature == 'Y') & (self.reg_winddirection == 'N'):
                    reg_inputs_por = pd.concat([self._reanalysis_por_avg[self._mc_reanalysis_product[n]], self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_temp']], axis = 1)
                    gross_por = ml_model_cv.predict(np.array(reg_inputs_por))
                elif (self.reg_temperature == 'N') & (self.reg_winddirection == 'Y'):
                    wd_sin = np.sin(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    wd_cos = np.cos(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    reg_inputs_por = pd.concat([self._reanalysis_por_avg[self._mc_reanalysis_product[n]], wd_sin, wd_cos], axis = 1)
                    gross_por = ml_model_cv.predict(np.array(reg_inputs_por))
                else:
                    wd_sin = np.sin(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    wd_cos = np.cos(2*np.pi* self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_wd']/360)
                    reg_inputs_por = pd.concat([self._reanalysis_por_avg[self._mc_reanalysis_product[n]], self._reanalysis_por_avg[self._mc_reanalysis_product[n] + '_temp'], wd_sin, wd_cos], axis = 1)
                    gross_por = ml_model_cv.predict(np.array(reg_inputs_por))
                                        
                # Get long-term availability and curtailment losses by month
                [avail_lt_losses, curt_lt_losses] = self.sample_long_term_losses(n)  
        
                # Assign AEP, long-term availability, and long-term curtailment to output data frame
                aep_GWh[n] = gross_lt.sum() * (1 - avail_lt_losses)
                avail_pct[n] = avail_lt_losses
                curt_pct[n] = curt_lt_losses
                lt_por_ratio[n] = gross_lt.sum() / gross_por.sum()
            
        # Return final output
        sim_results = pd.DataFrame(index=np.arange(num_sim), data={'aep_GWh': aep_GWh,                                                                                                        
                                                                   'avail_pct': avail_pct,                                                                                                      
                                                                   'curt_pct': curt_pct,                                                                                                    
                                                                   'lt_por_ratio': lt_por_ratio})      
        return sim_results

    @logged_method_call
    def sample_normal(self,unc):
        return np.random.normal(1,unc,1)

    @logged_method_call
    def sample_long_term_reanalysis(self, n, r):
        """
        This function returns the windiness-corrected monthly wind speeds based on the Monte-Carlo generated sample of:
            1. The reanalysis product
            2. The number of years to use in the long-term correction

        Args:
           n(:obj:`integer`): The number of years for the windiness correction
           r(:obj:`string`): The reanalysis product used for Monte Carlo sample 'n'

        Returns:
           :obj:`pandas.DataFrame`: the windiness-corrected or 'long-term' annualized monthly/daily wind speeds

        """

        ws_df = self._reanalysis_monthly_daily[r].to_frame().dropna()  # Drop NA values from monthly/daily reanalysis data series
        if self.reg_winddirection == 'Y': 
            u_df = self._reanalysis_monthly_daily[r + '_u'].to_frame().dropna()
            v_df = self._reanalysis_monthly_daily[r + '_v'].to_frame().dropna()
        if self.reg_temperature == 'Y': 
            temp_df = self._reanalysis_monthly_daily[r + '_temp'].to_frame().dropna()
        
        if self.time_resolution == 'M':
            ws_data = ws_df.tail(n * 12)  # Get last 'x' years of data from reanalysis product
            ws_monthly_daily = ws_data.groupby(ws_data.index.month)[r].mean()  # Get long-term annualized monthly wind speeds
            # IAV
            ws_monthly_daily_sd = ws_data.groupby(ws_data.index.month)[r].std() # Get long-term annualized stdev of monthly wind speeds            
            iav_df = ws_monthly_daily_sd/ws_monthly_daily
            iav_df = iav_df.to_frame()
            iav_df['sample'] = iav_df.apply(lambda row: self.sample_normal(row[r]), axis=1)
            mc_ws_monthly_daily = ws_monthly_daily * iav_df['sample']  
            
            if self.reg_temperature == 'Y': 
                temp_data = temp_df.tail(n * 12)
                temp_monthly_daily = temp_data.groupby(temp_data.index.month)[r + '_temp'].mean()
            if self.reg_winddirection == 'Y': 
                u_data = u_df.tail(n * 12)
                v_data = v_df.tail(n * 12)
                u_monthly_daily = u_data.groupby(u_data.index.month)[r + '_u'].mean()
                v_monthly_daily = v_data.groupby(v_data.index.month)[r + '_v'].mean()
                wd_monthly_daily = 180-np.rad2deg(np.arctan2(-u_monthly_daily,v_monthly_daily))
            
        elif self.time_resolution == 'D':   
            ws_data = ws_df.tail(n * 366)  # Get last 'x' years of data from reanalysis product
            ws_monthly_daily = ws_data.groupby([(ws_data.index.month),(ws_data.index.day)])[r].mean()  # Get long-term annualized daily wind speeds
            ws_monthly_daily.reset_index(drop=True, inplace=True)
            # IAV
            ws_monthly_daily_sd_12 = ws_data.groupby(ws_data.index.month)[r].std() # Get long-term annualized stdev of daily wind speeds            
            num_days_lt = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
            ws_monthly_daily_sd = np.repeat(ws_monthly_daily_sd_12, num_days_lt)
            ws_monthly_daily_sd.reset_index(drop=True, inplace=True)
            iav_df = ws_monthly_daily_sd/ws_monthly_daily
            iav_df = iav_df.to_frame()
            iav_df['sample'] = iav_df.apply(lambda row: self.sample_normal(row[r]), axis=1)
            mc_ws_monthly_daily = ws_monthly_daily * iav_df['sample']  
                        
            if self.reg_temperature == 'Y':
                temp_data = temp_df.tail(n * 366)
                temp_monthly_daily = temp_data.groupby([(temp_data.index.month),(temp_data.index.day)])[r + '_temp'].mean()
            if self.reg_winddirection == 'Y':
                u_data = u_df.tail(n * 366)
                v_data = v_df.tail(n * 366)
                u_monthly_daily = u_data.groupby([(u_data.index.month),(u_data.index.day)])[r + '_u'].mean()
                v_monthly_daily = v_data.groupby([(v_data.index.month),(v_data.index.day)])[r + '_v'].mean()
                wd_monthly_daily = 180-np.rad2deg(np.arctan2(-u_monthly_daily,v_monthly_daily))
        
        # Store result in dictionary
        long_term_reg_inputs = mc_ws_monthly_daily.astype(float).reset_index(drop=True)
        if self.reg_temperature == 'Y': 
            long_term_reg_inputs = pd.concat([mc_ws_monthly_daily.astype(float).reset_index(drop=True), temp_monthly_daily.astype(float).reset_index(drop=True)], axis=1)
        if self.reg_winddirection == 'Y':
            wd_sin_monthly_daily = np.sin(2*np.pi*wd_monthly_daily/360)
            wd_cos_monthly_daily = np.cos(2*np.pi*wd_monthly_daily/360)
            long_term_reg_inputs = pd.concat([long_term_reg_inputs.astype(float).reset_index(drop=True), wd_sin_monthly_daily.astype(float).reset_index(drop=True)], axis=1)
            long_term_reg_inputs = pd.concat([long_term_reg_inputs.astype(float).reset_index(drop=True), wd_cos_monthly_daily.astype(float).reset_index(drop=True)], axis=1)

        # Return result            
        return long_term_reg_inputs

    @logged_method_call
    def sample_long_term_losses(self, n):
        """
        This function calculates long-term availability and curtailment losses based on the Monte Carlo sampled
        historical availability and curtailment data

        Args:
            n(:obj:`integer`): The Monte Carlo iteration number

        Returns:
            :obj:`float`: annualized monthly/daily availability loss expressed as fraction
            :obj:`float`: annualized monthly/daily curtailment loss expressed as fraction
        """
        mc_avail = self.long_term_losses[0] * self._mc_loss_fraction[n]
        mc_curt = self.long_term_losses[1] * self._mc_loss_fraction[n]

        # Return availbilty and curtailment long-term monthly/daily data
        return mc_avail, mc_curt



