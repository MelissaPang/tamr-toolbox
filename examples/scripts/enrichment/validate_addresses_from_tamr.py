"""An example script to validate address data from Tamr and save results in Tamr"""
import argparse
from datetime import timedelta
from typing import Any, Dict, List

import tamr_toolbox as tbox
from tamr_toolbox.enrichment.address_validation import get_addr_to_validate
from tamr_toolbox.enrichment.api_client.google_address_validate import get_empty_address_validation
from tamr_toolbox.enrichment.enrichment_utils import join_clean_tuple


def main(
    *,
    instance_connection_info: Dict[str, Any],
    dataset_name: str,
    dataset_addr_columns: List[str],
    mapping_dataset_name: str,
    googlemaps_api_key: str,
    required_columns: List[str] = [],
) -> None:
    """Validate address data streamed from Tamr and save results on Tamr.

    Note that this does not update the dataset corresponding to the input `dataset_name` -- it
    performs lookups based on data in that dataset, and updates the dataset corresponding to the
    `mapping_dataset_name` with the new data.

    Args:
        instance_connection_info: Information for connecting to Tamr (host, port, username etc)
        dataset_name: name of the Tamr dataset containing the data to validate
        dataset_addr_columns: ordered list of columns in the unified dataset with address info
        mapping_dataset_name: name of the Tamr toolbox address validation mapping dataset
        googlemaps_api_key: API key for the Google Maps address validation API
        required_columns: list of columns which must be filled for validation to be attempted;
            entries without those columns will be filled with null values

    """
    # Make Tamr Client
    tamr = tbox.utils.client.create(**instance_connection_info)

    # Get dataframe from Tamr dataset.
    # For large datasets, it is to preferable to use a delta dataset with only unvalidated/expired
    # data. To do this, set up a SM project connected to current validated dataset and filter to
    # records with null/expired values in the validation columns
    dataset = tamr.datasets.by_name(dataset_name)
    df = tbox.data_io.dataframe.from_dataset(
        dataset, columns=dataset_addr_columns, flatten_delimiter=" | "
    )
    LOGGER.info("Read %s records from Tamr dataset %s.", len(df), dataset_name)

    if required_columns:
        for col in required_columns:
            df[col] = df[col].str.strip()
        mask = df[required_columns].all(axis=1) & ~df[required_columns].isnull().any(axis=1)
        rows_with_missing = df[-mask]
        df = df[mask]
        LOGGER.info("%s input records were missing required fields.", len(rows_with_missing))

    # Stream address mapping data from Tamr -- must match Toolbox AddressValidationMapping class
    mapping_dataset = tamr.datasets.by_name(mapping_dataset_name)
    mapping = tbox.enrichment.address_mapping.from_dataset(mapping_dataset)

    LOGGER.info("Starting address validation.")
    maps_client = tbox.enrichment.api_client.google_address_validate.get_maps_client(
        googlemaps_api_key
    )

    tuples = tbox.enrichment.enrichment_utils.dataframe_to_tuples(
        dataframe=df, columns_to_join=dataset_addr_columns
    )
    addr_strings = get_addr_to_validate(
        input_addresses=tuples, addr_mapping=mapping, expiration_date_buffer=timedelta(days=2)
    )

    # Update the `region_code` below to match the expected region of your dataset, or set it to
    # `None` if no region code can be inferred.
    # Update the expiration date buffer depending on update frequency of your pipeline
    mapping = tbox.enrichment.address_validation.from_list(
        addresses_to_validate=addr_strings,
        client=maps_client,
        dictionary=mapping,
        enable_usps_cass=False,
        region_code="US",
    )

    if required_columns:
        tuples = tbox.enrichment.enrichment_utils.dataframe_to_tuples(
            dataframe=rows_with_missing, columns_to_join=dataset_addr_columns
        )
        seen = set()
        for tup in set(tuples):
            addr = join_clean_tuple(tup)
            if addr not in seen:
                mapping[addr] = get_empty_address_validation(addr)
                seen.add(addr)

    # Update address validation mapping on Tamr
    dataset_name = tbox.enrichment.address_mapping.to_dataset(
        addr_mapping=mapping, dataset=mapping_dataset
    )
    LOGGER.info("Tamr dataset %s updated with new validation data", dataset_name)
    LOGGER.info("Script complete.")


if __name__ == "__main__":
    # Set up command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="path to a YAML configuration file", required=False)
    args = parser.parse_args()

    # Load the configuration from the file path provided or the default file path specified
    CONFIG = tbox.utils.config.from_yaml(
        path_to_file=args.config,
        default_path_to_file="/path/to/my/conf/address_validation.config.yaml",
    )

    # Use the configuration to create a global logger
    LOGGER = tbox.utils.logger.create(__name__, log_directory=CONFIG["logging_dir"])
    tbox.utils.logger.enable_package_logging("tamr_toolbox", log_directory=CONFIG["logging_dir"])
    tbox.utils.logger.enable_package_logging(
        "tamr_unify_client", log_directory=CONFIG["logging_dir"]
    )

    # Run the main function
    main(
        instance_connection_info=CONFIG["my_tamr_instance"],
        dataset_name=CONFIG["datasets"]["my_dataset_to_be_addr_validated"]["name"],
        dataset_addr_columns=CONFIG["datasets"]["my_dataset_to_be_addr_validated"]["address_cols"],
        mapping_dataset_name=CONFIG["datasets"]["my_addr_validation_mapping"]["name"],
        googlemaps_api_key=CONFIG["address_validation"]["googlemaps_api_key"],
        required_columns=CONFIG["datasets"]["my_dataset_to_be_addr_validated"]["required_cols"],
    )
