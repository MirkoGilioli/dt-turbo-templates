# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import pathlib

from kfp.v2 import compiler, dsl
from google_cloud_pipeline_components.aiplatform import (
    ModelBatchPredictOp,
)

from pipelines import generate_query
from pipelines.components import (
    lookup_model,
    extract_bq_to_dataset,
    bq_query_to_table,
    load_dataset_to_bq,
    validate_skew,
    generate_statistics,
    visualise_statistics,
    show_anomalies,
)


@dsl.pipeline(name="tensorflow-prediction-pipeline")
def tensorflow_pipeline(
    project_id: str = os.environ.get("VERTEX_PROJECT_ID"),
    project_location: str = os.environ.get("VERTEX_LOCATION"),
    pipeline_files_gcs_path: str = os.environ.get("PIPELINE_FILES_GCS_PATH"),
    ingestion_project_id: str = os.environ.get("VERTEX_PROJECT_ID"),
    tfdv_schema_filename: str = "tfdv_schema_serving.pbtxt",
    tfdv_train_stats_path: str = os.environ.get("TRAIN_STATS_GCS_PATH"),
    model_name: str = "tensorflow_with_preprocessing",
    model_label: str = "label_name",
    dataset_id: str = "preprocessing",
    dataset_location: str = os.environ.get("VERTEX_LOCATION"),
    ingestion_dataset_id: str = "chicago_taxi_trips",
    timestamp: str = "2022-12-01 00:00:00",
    batch_prediction_machine_type: str = "n1-standard-4",
    batch_prediction_min_replicas: int = 3,
    batch_prediction_max_replicas: int = 10,
):
    """
    Tensorflow prediction pipeline which:
     1. Extracts a dataset from BQ
     2. Validates training/serving skew
     3. Scores data to produce predictions
     4. Post-processes predictions
     5. Loads predictions into BQ

    Args:
        project_id (str): project id of the Google Cloud project
        project_location (str): location of the Google Cloud project
        pipeline_files_gcs_path (str): GCS path where the pipeline files are located
        ingestion_project_id (str): project id containing the source bigquery data
            for ingestion. This can be the same as `project_id` if the source data is
            in the same project where the ML pipeline is executed.
        model_name (str): name of model
        model_label (str): label of model
        tfdv_schema_filename (str): filename of schema generated by tfdv
            (in assets directory)
        tfdv_train_stats_path (str): path for statistics generated by tfdv
        dataset_id (str): id of BQ dataset used to store all staging data & predictions
        dataset_location (str): location of dataset
        ingestion_dataset_id (str): dataset id of ingestion data
        timestamp (str): Optional. Empty or a specific timestamp in ISO 8601 format
            (YYYY-MM-DDThh:mm:ss.sss±hh:mm or YYYY-MM-DDThh:mm:ss).
            If any time part is missing, it will be regarded as zero.
        batch_prediction_machine_type (str): Machine type to be used for Vertex Batch
            Prediction. Example machine_types - n1-standard-4, n1-standard-16 etc
        batch_prediction_min_replicas (int): Minimum no of machines to distribute the
            Vertex Batch Prediction job for horizontal scalability
        batch_prediction_max_replicas (int): Maximum no of machines to distribute the
            Vertex Batch Prediction job for horizontal scalability.


    Returns:
        None

    """

    # Create variables to ensure the same arguments are passed
    # into different components of the pipeline
    file_pattern = ""  # e.g. "files-*.csv", used as file pattern on storage
    time_column = "trip_start_timestamp"
    ingestion_table = "taxi_trips"
    table_suffix = "_tf_prediction"  # suffix to table names
    ingested_table = "ingested_data" + table_suffix

    # generate sql queries which are used in ingestion and preprocessing
    # operations
    queries_folder = pathlib.Path(__file__).parent / "queries"

    ingest_query = generate_query(
        queries_folder / "ingest.sql",
        source_dataset=f"{ingestion_project_id}.{ingestion_dataset_id}",
        source_table=ingestion_table,
        filter_column=time_column,
        filter_start_value=timestamp,
    )

    # data ingestion and preprocessing operations
    kwargs = dict(
        bq_client_project_id=project_id,
        destination_project_id=project_id,
        dataset_id=dataset_id,
        dataset_location=dataset_location,
        query_job_config=json.dumps(dict(write_disposition="WRITE_TRUNCATE")),
    )
    ingest = bq_query_to_table(
        query=ingest_query, table_id=ingested_table, **kwargs
    ).set_display_name("Ingest data")

    # data extraction to gcs
    data_for_prediction = (
        extract_bq_to_dataset(
            bq_client_project_id=project_id,
            source_project_id=project_id,
            dataset_id=dataset_id,
            table_name=ingested_table,
            dataset_location=dataset_location,
            extract_job_config=json.dumps(
                dict(destination_format="NEWLINE_DELIMITED_JSON")
            ),
            file_pattern=file_pattern,
        )
        .after(ingest)
        .set_display_name("Extract data to storage for prediction")
    )

    data_for_validation = (
        extract_bq_to_dataset(
            bq_client_project_id=project_id,
            source_project_id=project_id,
            dataset_id=dataset_id,
            table_name=ingested_table,
            dataset_location=dataset_location,
            extract_job_config=json.dumps(dict(destination_format="CSV")),
            file_pattern=file_pattern,
        )
        .after(ingest)
        .set_display_name("Extract data to storage for validation")
    )

    # validate training/serving skew
    serving_stats = generate_statistics(
        data_for_validation.outputs["dataset"],
        file_pattern=file_pattern,
    ).set_display_name("Generate data statistics")
    # visualise statistics
    visualised_statistics = visualise_statistics(
        statistics=serving_stats.output,
        statistics_name="Serving Statistics",
        other_statistics_path=tfdv_train_stats_path,
        other_statistics_name="Training Statistics",
    ).set_display_name("Visualise data statistics")

    # Construct schema_path from base GCS path + filename
    tfdv_schema_path = (
        f"{pipeline_files_gcs_path}/prediction/assets/{tfdv_schema_filename}"
    )

    validated_skew = validate_skew(
        training_statistics_path=tfdv_train_stats_path,
        schema_path=tfdv_schema_path,
        serving_statistics=serving_stats.output,
        environment="SERVING",
    ).set_display_name("Validate data skew")

    anomalies = show_anomalies(
        anomalies=validated_skew.output, fail_on_anomalies=True
    ).set_display_name("Show anomalies")

    # lookup champion model
    champion_model = lookup_model(
        model_name=model_name,
        model_label=model_label,
        project_location=project_location,
        project_id=project_id,
        fail_on_model_not_found=True,
    ).set_display_name("Lookup champion model")

    # predict data
    gcs_source_uris = data_for_prediction.outputs["dataset_gcs_uri"]
    gcs_destination_output_uri_prefix = data_for_prediction.outputs[
        "dataset_gcs_prefix"
    ]

    batch_prediction = (
        ModelBatchPredictOp(
            project=project_id,
            job_display_name="my-tensorflow-batch-prediction-job",
            location=project_location,
            model=champion_model.outputs["model"],
            instances_format="jsonl",
            predictions_format="jsonl",
            gcs_source_uris=gcs_source_uris,
            gcs_destination_output_uri_prefix=gcs_destination_output_uri_prefix,
            machine_type=batch_prediction_machine_type,
            starting_replica_count=batch_prediction_min_replicas,
            max_replica_count=batch_prediction_max_replicas,
        )
        .after(anomalies)
        .set_display_name("Vertex Batch Predictions for TF model")
    )

    # load predictions into bigquery
    loaded_data = (
        load_dataset_to_bq(
            bq_client_project_id=project_id,
            destination_project_id=project_id,
            dataset_id=dataset_id,
            table_name="tensorflow_staging_predictions",
            dataset=batch_prediction.outputs["batchpredictionjob"],
            dataset_location=dataset_location,
        )
        .after(batch_prediction)
        .set_display_name("Load predictions into Bigquery")
    )


def compile():
    """
    Uses the kfp compiler package to compile the pipeline function into a workflow yaml

    Args:
        None

    Returns:
        None
    """
    compiler.Compiler().compile(
        pipeline_func=tensorflow_pipeline,
        package_path="prediction.json",
        type_check=False,
    )


if __name__ == "__main__":
    compile()
