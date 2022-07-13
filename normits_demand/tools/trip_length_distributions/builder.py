# -*- coding: utf-8 -*-
"""
Created on: 26/08/2020
Updated on:

Original author: Chris Storey
Last update made by: Ben Taylor
Other updates made by:

File purpose:

"""
# Built-Ins
import os
import pathlib

from typing import Any

# Third Party
import numpy as np
import pandas as pd

# Local Imports
# pylint: disable=import-error,wrong-import-position
import normits_demand as nd

from normits_demand import cost
from normits_demand import core as nd_core
from normits_demand import logging as nd_log
from normits_demand import constants as nd_consts
from normits_demand.utils import file_ops
from normits_demand.utils import timing
from normits_demand.utils import functional as func_utils
from normits_demand.utils import string_utils as str_utils
from normits_demand.utils import pandas_utils as pd_utils
from normits_demand.tools.trip_length_distributions import enumerations as tld_enums
# pylint: enable=import-error,wrong-import-position

LOG = nd_log.get_logger(__name__)


class TripLengthDistributionBuilder:
    # Class constants
    _running_log_fname = "1. input_params.txt"
    _full_export_fname = "2. full_export.csv"
    _seg_report_fname = "3. {seg_name}_report.csv"

    # Maps for non-classified categories
    _household_type_to_ca = {
        "hh_type": [1, 2, 3, 4, 5, 6, 7, 8],
        "ca": [1, 2, 1, 2, 2, 1, 2, 2],
    }

    def __init__(
        self,
        tlb_folder: nd.PathLike,
        tlb_version: nd.PathLike,
        output_folder: nd.PathLike,
        bands_definition_dir: str,
        segment_copy_definition_dir: str,
        trip_miles_col: str = "trip_mile",
        trip_count_col: str = "trips",
    ):
        """
        Define the environment for a set of trip length distribution runs.

        Parameters
        ----------
        tlb_folder: pd.DataFrame
            Path to folder containing TLD specific output from
            'NTS Processing' tool
        tlb_version: str
            Which version of the TLD export to pick up
        output_folder:
            NorMITs Demand config folder to export outputs to
        trip_miles_col:
            Which column to use as the trip miles in the import data
        trip_count_col:
            Which column to use as the count of trips in the import data
        """
        # TODO(BT): Pass this in
        self.input_cost_units = nd_core.CostUnits.MILES

        self.bands_definition_dir = bands_definition_dir
        self.segment_copy_definition_dir = segment_copy_definition_dir

        self.tlb_folder = tlb_folder
        self.tlb_version = tlb_version
        self.tlb_import_path = os.path.join(tlb_folder, tlb_version)
        self.output_folder = output_folder

        print(f"Loading processed NTS trip length data from {self.tlb_import_path}...")
        self.nts_import = pd.read_csv(self.tlb_import_path)

        # Validate needed columns exist
        if trip_miles_col not in list(self.nts_import):
            raise ValueError(f"Given trip miles col {trip_miles_col} not in NTS data")
        if trip_count_col not in list(self.nts_import):
            raise ValueError(f"Given trip count col {trip_count_col} not in NTS data")

        self.trip_distance_col = trip_miles_col
        self.trip_count_col = trip_count_col

    def _apply_od_geo_filter(
        self,
        nts_data: pd.DataFrame,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
    ) -> pd.DataFrame:
        """Internal function of self._apply_geo_filter"""
        # Init
        output_data = nts_data.copy()
        orig_gor_col = "TripOrigGOR_B02ID"
        dest_gor_col = "TripDestGOR_B02ID"

        # Transpose origin and destination for OD_to trip ends (Makes them "PA")
        mask = output_data[self.trip_origin_col] == "hb_to"
        temp_orig = output_data[mask][orig_gor_col].copy()
        temp_dest = output_data[mask][dest_gor_col].copy()
        output_data.loc[mask, orig_gor_col] = temp_dest
        output_data.loc[mask, dest_gor_col] = temp_orig

        # Decide how to filter
        if trip_filter_type == tld_enums.TripFilter.TRIP_OD:
            filter_orig = True
            filter_dest = True
        elif trip_filter_type == tld_enums.TripFilter.TRIP_O:
            filter_orig = True
            filter_dest = False
        elif trip_filter_type == tld_enums.TripFilter.TRIP_D:
            filter_orig = False
            filter_dest = True
        else:
            raise ValueError(f"Don't know how to apply the OD filter {trip_filter_type}")

        # Finally, filter the data
        if filter_orig:
            mask = output_data[orig_gor_col].isin(geo_area.get_gors())
            output_data = output_data[mask].reset_index(drop=True)

        if filter_dest:
            mask = output_data[dest_gor_col].isin(geo_area.get_gors())
            output_data = output_data[mask].reset_index(drop=True)

        return output_data

    def _apply_geo_filter(
        self,
        nts_data: pd.DataFrame,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
    ) -> pd.DataFrame:
        """Filters the data to a geographical area

        Parameters
        ----------
        nts_data: pd.DataFrame
            Processed NTS data

        trip_filter_type: str
            How to filter the origin/destination of trips.
            this is defined here in the class, so improvement work here.

        geo_area:
            Target regional subset

        Returns
        ----------
        output_dat:
            Input dataframe modified to target geography
        """
        # TODO(CS): Work for household trip_filter_type properly

        if trip_filter_type.is_od_type():
            return self._apply_od_geo_filter(
                nts_data=nts_data,
                geo_area=geo_area,
                trip_filter_type=trip_filter_type,
            )

        raise ValueError(f"Don't know how to apply to filter '{trip_filter_type}'")

    @staticmethod
    def _map_dict(output_dat: pd.DataFrame, map_dict: dict, key: str):

        """
        Analogue of pd.map for filling out a category from a dictionary
        output_dat: a DataFrame of NTS dataset
        map: a dictionary in category: list format
        key: string for join, existing category in output_dat
        """

        map_frame = pd.DataFrame(map_dict)

        output_dat = output_dat.merge(map_frame, how="left", on=key)

        return output_dat

    def _build_cost_distribution(
        self,
        data: pd.DataFrame,
        band_edges: np.ndarray,
        output_cost_units: nd_core.CostUnits,
        trip_count_col: str = None,
        trip_dist_col: str = None,
    ) -> cost.CostDistribution:
        """Generates a CostDistribution from a dataframe of data and bands

        Distributes `data` between `band_edges`,
        counting trips per band and mean trip length per band.

        Parameters
        ----------
        data:
            The data to convert into a `CostDistribution`. Must contain the
            columns: `trip_count_col`, `trip_miles_col`

        band_edges:
            The edges to use for each band in the distribution. E.g.
            `[1, 2, 3]` defines 2 bands: 1->2 and 2->3

        output_cost_units:
            The cost units to convert the data to from `self.input_cost_units`

        trip_count_col:
            The name of the column in `data` containing the count of trips
            being made.

        trip_dist_col:
            The name of the column in `data` containing the distance of trips
            being made. This should be in `self.input_cost_units`

        Returns
        -------
        cost_distribution:
            A `CostDistribution` storing all the cost distribution data
        """
        # Init
        data = data.copy()
        sample_size = len(data)
        trip_count_col = self.trip_count_col if trip_count_col is None else trip_count_col
        trip_dist_col = self.trip_distance_col if trip_dist_col is None else trip_dist_col

        # Convert distances to output cost
        conv_factor = self.input_cost_units.get_conversion_factor(output_cost_units)
        data[trip_dist_col] *= conv_factor

        # Calculate values for each band
        all_band_trips = list()
        all_band_mean_cost = list()
        for lower, upper in func_utils.pairwise(band_edges):
            # Filter to trips in this band
            mask = (data[trip_dist_col] > lower) & (data[trip_dist_col] < upper)
            band_data = data[mask].copy()

            # Calculate mean miles and total trips
            band_trips = band_data[trip_count_col].values.sum()
            band_distance = np.sum(
                band_data[trip_count_col].values * band_data[trip_dist_col].values
            )
            if band_distance <= 0:
                band_mean_cost = np.mean([lower, upper])
            else:
                band_mean_cost = band_distance / band_trips

            all_band_trips.append(band_trips)
            all_band_mean_cost.append(band_mean_cost)

        return cost.CostDistribution(
            edges=band_edges,
            band_trips=np.array(all_band_trips),
            cost_units=output_cost_units,
            band_mean_cost=np.array(all_band_mean_cost),
            sample_size=sample_size,
        )

    def _filter_nts_data(
        self,
        nts_data: pd.DataFrame,
        segment_params: dict[str, Any],
    ) -> pd.DataFrame:
        """Filters to keep only the segments needed

        Handles all special cases on how to use the `segment_params` to filter
        the `nts_data`.

        Parameters
        ----------
        nts_data:
            A dataframe of the data to filter.

        segment_params:
            The `{col_names: col_value}` pairs to filter `nts_data` with.
            Note that internally, this function handles the following `col_names`
            specially:
            ["trip_origin", "uc", "soc", "ns"]

        Returns
        -------
        filtered_nts_data:
            The original `nts_data` filtered to only include the selected
            segments. Return will be a copied and filtered version of `nts_data`.
        """
        # Init
        to_dict = {"hb": ["hb_to", "hb_from"], "nhb": ["nhb"]}

        # Build the filter
        df_filter = dict()
        for seg_name, seg_val in segment_params.items():

            # Deal with special cases
            if seg_name == "trip_origin":
                df_filter[seg_name] = to_dict[seg_val]

            elif seg_name == "uc":
                uc_enum = nd_core.UserClass(seg_val)
                df_filter[self.purpose_col] = uc_enum.get_purposes()

            elif seg_name in ("soc", "ns"):
                # Don't filter if Nan. Invalid segment
                if not np.isnan(seg_val):
                    df_filter[seg_name] = seg_val

            else:
                # Assume we can filter as normal
                df_filter[seg_name] = seg_val

        # Apply
        return pd_utils.filter_df(df=nts_data, df_filter=df_filter)

    @staticmethod
    def _generate_tld_name(
        segmentation: nd_core.SegmentationLevel,
        segment_params: dict[str, Any],
    ) -> str:
        """Wrapper around `segmentation.generate_file_name()` for consistent filenames"""
        trip_origin = segment_params.get("trip_origin", None)
        return segmentation.generate_file_name(
            segment_params=segment_params,
            trip_origin=trip_origin,
            file_desc="cost_distribution",
        )

    def _handle_sample_period(
        self,
        input_dat: pd.DataFrame,
        sample_period: tld_enums.SampleTimePeriods,
    ) -> pd.DataFrame:
        """
        Function to subset whole dataset for time periods
        """
        keep_tps = sample_period.get_time_periods()
        output_dat = input_dat[input_dat[self.tp_col].isin(keep_tps)]
        output_dat = output_dat.reset_index(drop=True)
        return output_dat

    @staticmethod
    def _get_name_and_fname(band_or_seg_name: str) -> tuple[str, str]:
        if ".csv" in band_or_seg_name:
            fname = band_or_seg_name
            name = band_or_seg_name.replace(".csv", "")
        else:
            fname = f"{band_or_seg_name}.csv"
            name = band_or_seg_name
        return fname, name

    def build_output_path(
        self,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
        bands_name: str,
        segmentation: nd_core.SegmentationLevel,
        sample_period: tld_enums.SampleTimePeriods,
        cost_units: nd_core.CostUnits,
    ) -> pathlib.Path:
        """Generates the output path for the TLD params

        Parameters
        ----------
        geo_area:
            The geographical area the TLD is constrained to.

        trip_filter_type:
            How to filter the trips into given `geo_area`.

        bands_name:
            The name of the bands being used in the TLD.

        segmentation:
            The segmentation being used in the TLD.

        sample_period:
            Which time periods the TLD is restricted to.

        cost_units:
            The cost units used in the output of the TLDs.

        Returns
        -------
        path_string:
            A string. The full path to a folder where this collection of TLDs
            should be stored.
        """
        # Make sure band and segment names are correct
        _, bands_name = self._get_name_and_fname(bands_name)

        return pathlib.Path(
            os.path.join(
                self.output_folder,
                geo_area.value,
                trip_filter_type.value,
                bands_name,
                segmentation.name,
                sample_period.value,
                cost_units.value,
            )
        )

    def generate_output_paths(
        self,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
        bands_name: str,
        segmentation: nd_core.SegmentationLevel,
        sample_period: tld_enums.SampleTimePeriods,
        cost_units: nd_core.CostUnits,
    ) -> tuple[pathlib.Path, list[str]]:
        """Generates all the file output paths for the TLD params

        Parameters
        ----------
        geo_area:
            The geographical area the TLD is constrained to.

        trip_filter_type:
            How to filter the trips into given `geo_area`.

        bands_name:
            The name of the bands being used in the TLD.

        segmentation:
            The segmentation to generate a group of TLDs for

        sample_period:
            Which time periods the TLD is restricted to.

        cost_units:
            The cost units used in the output of the TLDs.

        Returns
        -------
        path_string:
            A string. The full path to a folder where this collection of TLDs
            should be stored.
        """
        # Generate the directory all the files are output
        base_path = self.build_output_path(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
        )

        # Generate the filenames
        fnames = list()
        for segment_params in segmentation:
            tld_name = self._generate_tld_name(segmentation, segment_params)
            fnames.append(f"{tld_name}.csv")

        return base_path, fnames

    def copy_across_tps(
        self,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
        bands_name: str,
        segmentation: nd_core.SegmentationLevel,
        sample_period: tld_enums.SampleTimePeriods,
        cost_units: nd_core.CostUnits,
        process_count: int = nd_consts.PROCESS_COUNT,
    ) -> None:
        """Copies generated TLDs across time period segments

        This is useful when segments have had to be aggregated due to sample
        sizes, but other models and tools expect the inputs to be at
        a time period segmentation.

        Parameters
        ----------
        geo_area:
            The geographical area the TLD is constrained to.

        trip_filter_type:
            How to filter the trips into given `geo_area`.

        bands_name:
            The name of the bands being used in the TLD.

        segmentation:
            The segmentation being used in the TLD.

        sample_period:
            Which time periods the TLD is restricted to.

        cost_units:
            The cost units used in the output of the TLDs.

        process_count:
            The number of processes to use when copying the data over.
            0 - use no multiprocessing, run as a loop.
            +ve value - the number of processes to use.
            -ve value - the number of processes less than the cpu count to use.

        Returns
        -------
        None
        """
        # Generate the needed paths
        base_path, fnames = self.generate_output_paths(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
        )

        output_path = base_path / "tp_copied"
        file_ops.create_folder(output_path)

        # Copy across time periods
        copy_files = list()
        for fname in fnames:
            for tp in sample_period.get_time_periods():
                out_name = fname.replace(".csv", f"_tp{tp}.csv")

                # Tuple of src, dst files
                copy_files.append(
                    (
                        base_path / fname,
                        output_path / out_name,
                    )
                )

        file_ops.copy_and_rename_files(files=copy_files, process_count=process_count)

        # Write out a log of what happened
        run_log_str = self.generate_run_log(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
            tp_copy=True,
        )

        output_path = output_path / self._running_log_fname
        with open(output_path, "w") as f:
            f.write(run_log_str)

    def copy_tlds(
        self,
        copy_definition_name: str,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
        bands_name: str,
        segmentation: nd_core.SegmentationLevel,
        sample_period: tld_enums.SampleTimePeriods,
        cost_units: nd_core.CostUnits,
        process_count: int = nd_consts.PROCESS_COUNT,
    ) -> None:
        """Copies generated TLDs across multiple segments

        This is useful when segments have had to be aggregated due to sample
        sizes, but other models and tools expect the inputs to be at
        the original segmentation.

        Parameters
        ----------
        copy_definition_name:
            The name of the copy definition to use to copy the files

        geo_area:
            The geographical area the TLD is constrained to.

        trip_filter_type:
            How to filter the trips into given `geo_area`.

        bands_name:
            The name of the bands being used in the TLD.

        segmentation:
            The segmentation being used in the TLD.

        sample_period:
            Which time periods the TLD is restricted to.

        cost_units:
            The cost units used in the output of the TLDs.

        process_count:
            The number of processes to use when copying the data over.
            0 - use no multiprocessing, run as a loop.
            +ve value - the number of processes to use.
            -ve value - the number of processes less than the cpu count to use.

        Returns
        -------
        None
        """
        # Read in the copy definition
        fname, copy_definition_name = self._get_name_and_fname(copy_definition_name)
        copy_def = pd.read_csv(os.path.join(self.segment_copy_definition_dir, fname))

        # Generate the directory all the files are output
        base_path = self.build_output_path(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
        )

        # Build the output path
        output_path = base_path / copy_definition_name
        file_ops.create_folder(output_path)

        file_ops.copy_defined_files(
            copy_definition=copy_def,
            src_dir=base_path,
            dst_dir=output_path,
            process_count=process_count,
        )

        # Write out a log of what happened
        run_log_str = self.generate_run_log(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
            copy_definition_name=copy_definition_name,
        )

        output_path = output_path / self._running_log_fname
        with open(output_path, "w") as f:
            f.write(run_log_str)

    def build_tld(
        self,
        tld_data: pd.DataFrame,
        bands: pd.DataFrame,
        segmentation: nd_core.SegmentationLevel,
        cost_units: nd_core.CostUnits,
        sample_threshold: int = 10,
    ) -> tuple[dict[str, cost.CostDistribution], pd.DataFrame, pd.DataFrame]:
        """
        Build a set of trip length distributions

        Parameters
        ----------
        tld_data:
            Dataframe of pre-processed trip length distribution data

        bands: pd.DataFrame:
            Dataframe of bands with headings lower, upper

        segmentation:
            The segmentation to generate a group of TLDs for

        cost_units:
            The cost units to use in the output. The data will be multiplied
            by a constant to convert.

        sample_threshold: int = 10:
            Sample size below which skip allocation to bands and fail out

        Returns
        ----------
        name_to_distribution:
            A dictionary with {tld_name: CostDistribution}

        sample_size_log:
            input segments with reported number of records and status

        full_export:
            A pandas dataframe containing all the generated TLDs alongside
            their segment_params
        """
        # Init
        name_to_distribution = dict()
        sample_size_log = list()
        full_export = list()

        # Generate a TLD for each segment
        for segment_params in segmentation:
            # Filter to data for this segment
            data_subset = tld_data.copy()
            data_subset = self._filter_nts_data(data_subset, segment_params)
            sample_size = len(data_subset)

            # Build the sample size log
            log_line = segment_params.copy()
            log_line["records"] = sample_size

            if sample_size < sample_threshold:
                log_line["status"] = "Failed"
                LOG.warning(
                    "Not enough data was returned to create a TLD for segment "
                    f"{segment_params}. Lower limit set to {sample_threshold}, "
                    f"but only {sample_size} were found. No TLD will be generated."
                )
                continue
            log_line["status"] = "Passed"
            sample_size_log.append(log_line)

            # Build into a cost distribution
            min_bounds = bands["lower"].values
            max_bounds = bands["upper"].values
            tld = self._build_cost_distribution(
                data=data_subset,
                band_edges=np.array([min_bounds[0]] + max_bounds.tolist()),
                output_cost_units=cost_units,
            )

            # Add to the full export
            full_export.append(tld.to_df(additional_cols=segment_params))

            # Add to dictionary
            tld_name = self._generate_tld_name(segmentation, segment_params)
            name_to_distribution.update({tld_name: tld})

        # Consolidate reports
        full_export = pd.concat(full_export, ignore_index=True)
        sample_size_log = pd.DataFrame(sample_size_log)

        return name_to_distribution, sample_size_log, full_export

    def generate_run_log(
        self,
        geo_area: tld_enums.GeoArea,
        trip_filter_type: tld_enums.TripFilter,
        bands_name: str,
        segmentation: nd_core.SegmentationLevel,
        sample_period: tld_enums.SampleTimePeriods,
        cost_units: nd_core.CostUnits,
        copy_definition_name: str = None,
        tp_copy: bool = False,
    ) -> str:
        """Writes out a file of the params used to run

        Parameters
        ----------
        geo_area:
            The geographical area the TLD is constrained to.

        trip_filter_type:
            How to filter the trips into given `geo_area`.

        bands_name:
            The name of the bands being used in the TLD.

        segmentation:
            The segmentation being used in the TLD.

        sample_period:
            Which time periods the TLD is restricted to.

        cost_units:
            The cost units used in the output of the TLDs.

        copy_definition_name:
            Used when running `self.copy_tlds()` adds a line detailing the
            copy

        tp_copy:
            Used when running `self.copy_across_tps()`. Adds a line detailing
            the copy.

        Returns
        -------
        None
        """
        # Generate the lines of the log
        lines = list()
        lines.append(f"{str_utils.title_padding('TLD Tool Input Params and Run Log')}\n")
        lines.append(f"Code Version: {nd.__version__}")
        lines.append(f"Ran at: {timing.get_datetime()}\n")
        lines.append(f"Using the Classified Build from:\n{self.tlb_import_path}\n")
        lines.append("Input Params")
        lines.append("-" * 12)
        lines.append(f"Geo Area: {geo_area.value}")
        lines.append(f"Trip Filter Type: {trip_filter_type.value}")
        lines.append(f"Bands Name: {bands_name}")
        lines.append(f"Segmentation Name: {segmentation.name}")
        lines.append(f"Sample Time Periods: {sample_period.value}")
        lines.append(f"Output Cost Units: {cost_units.value}")

        if copy_definition_name is not None:
            fname, copy_definition_name = self._get_name_and_fname(copy_definition_name)
            copy_def_path = os.path.join(self.segment_copy_definition_dir, fname)

            lines.append(
                f"\nFiles were then copied to {copy_definition_name} segmentation "
                "using the definition:"
            )
            lines.append(f"{copy_def_path}")

        if tp_copy:
            lines.append("\nFiles were then copied across all time periods")

        return "\n".join(lines)

    def tld_generator(
        self,
        bands_name: str,
        segmentation: nd_core.SegmentationLevel,
        geo_area: tld_enums.GeoArea,
        sample_period: tld_enums.SampleTimePeriods,
        trip_filter_type: tld_enums.TripFilter,
        cost_units: nd_core.CostUnits = nd_core.CostUnits.MILES,
        sample_threshold: int = 10,
    ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        """Generate a trip length distribution

        Parameters
        ----------
        bands_name:
            Name of the bands to use, as named in self.bands_definition_dir
            # TODO(BT): Make this an object

        segmentation:
            The segmentation to generate a group of TLDs for

        geo_area:
            The geographical area to limit the generated TLD to

        sample_period:
            Time period filter for the generated TLD

        trip_filter_type:
            How to filter the trips into the geographical area.
            TRIP_OD will filter the origin and destination of trips into the
            defined `gep_area`.
            TRIP_O will only filter the origins
            TRIP_O will only filter the destinations

        cost_units:
            The cost units to use in the output. The data will be multiplied
            by a constant to convert.

        sample_threshold: int = 10:
            Sample below which to not bother running an application to bands
            Smallest possible number you would consider representative
            Failures captured in output sample_size_log

        verbose: bool = True,
            Echo to terminal or not

        Returns
        ----------
        name_to_distribution: dict
            A dictionary with {description of tld: tld DataFrame}
        full_export: pd.DataFrame
            A compiled, concatenated version of the DataFrames in name_to_distribution

        Future Improvements
        ----------
        Add more functionality for time period handling.
        Add better error control and type limiting for inputs.
        """
        # TODO(BT): Pass in - NEED to rewrite class first!
        self.purpose_col = "p"
        self.tp_col = "tp"
        self.trip_origin_col = "trip_origin"
        nts_to_segment_names = {
            "main_mode": "m",
            "p": self.purpose_col,
            "gender": "g",
            "soc": "soc",
            "ns": "ns",
            "start_time": self.tp_col,
            "trip_direction": self.trip_origin_col,
        }

        # Rename the input data to expected column names
        nts_data = self.nts_import.rename(columns=nts_to_segment_names)

        # Build output path
        tld_out_path = self.build_output_path(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
        )
        graph_out_path = tld_out_path / "graphs"
        file_ops.create_folder(graph_out_path, verbose_create=False)
        print(f"Generating TLD at: {tld_out_path}...")

        # Try read in the bands and segmentation
        fname, bands_name = self._get_name_and_fname(bands_name)
        bands = pd.read_csv(os.path.join(self.bands_definition_dir, fname))

        # Map categories not classified in classified build
        # Car availability
        # TODO: This should be handled upstream, in inputs
        nts_data = self._map_dict(
            output_dat=nts_data,
            map_dict=self._household_type_to_ca,
            key="hh_type",
        )

        # Filter data down to the time periods and geo area
        nts_data = self._handle_sample_period(nts_data, sample_period=sample_period)
        nts_data = self._apply_geo_filter(
            nts_data,
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
        )

        # Build tld dictionary, return a proper name for the distributions
        name_to_distribution, sample_size_log, full_export = self.build_tld(
            tld_data=nts_data,
            bands=bands,
            segmentation=segmentation,
            cost_units=cost_units,
            sample_threshold=sample_threshold,
        )

        # ## WRITE THINGS OUT ## #
        # Write the run log
        run_log_str = self.generate_run_log(
            geo_area=geo_area,
            trip_filter_type=trip_filter_type,
            bands_name=bands_name,
            segmentation=segmentation,
            sample_period=sample_period,
            cost_units=cost_units,
        )

        output_path = tld_out_path / self._running_log_fname
        with open(output_path, "w") as f:
            f.write(run_log_str)

        # Write sample_size_log
        _seg_report_fname = self._seg_report_fname.format(seg_name=segmentation.name)
        report_path = tld_out_path / _seg_report_fname
        file_ops.safe_dataframe_to_csv(sample_size_log, report_path, index=False)

        # Write final compiled tld
        full_export_path = tld_out_path / self._full_export_fname
        file_ops.safe_dataframe_to_csv(full_export, full_export_path, index=False)

        # Write individual tlds
        for name, tld in name_to_distribution.items():
            # Csv
            path = tld_out_path / f"{name}.csv"
            tld.to_csv(path)

            # Graph
            path = graph_out_path / f"{name}.png"
            tld.to_graph(path, band_shares=True, plot_title=name)

        return name_to_distribution, full_export
