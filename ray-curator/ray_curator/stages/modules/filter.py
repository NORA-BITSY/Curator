# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections.abc import Callable
from dataclasses import dataclass

import fasttext
import pandas as pd

from ray_curator.backends.base import WorkerMetadata
from ray_curator.stages.base import ProcessingStage
from ray_curator.stages.filters.doc_filter import DocumentFilter
from ray_curator.tasks import DocumentBatch


@dataclass
class Score(ProcessingStage[DocumentBatch, DocumentBatch]):
    """
    The module responsible for adding metadata to records based on statistics about the text.
    It accepts an arbitrary scoring function that accepts a text field and returns a score.
    It also accepts a DocumentFilter object, in which case the score_fn will be the score_document method of the DocumentFilter.

    Unlike ScoreFilter, it does not filter based on the computed score.
    It only adds metadata to the record.

    Args:
        score_fn (Callable | DocumentFilter): The score function or the DocumentFilter object. If it is a DocumentFilter object, the score_fn will be the score_document method of the DocumentFilter.
        score_field (str): The field the score will be stored in.
        text_field (str): The field the documents will be read from.

    """

    score_fn: Callable | DocumentFilter
    score_field: str
    text_field: str = "text"
    
    @property
    def name(self) -> str:
        return self.score_fn.name if isinstance(self.score_fn, DocumentFilter) else "score_fn"

    def inputs(self) -> tuple[list[str], list[str]]:
        return ["data"], [self.text_field]

    def outputs(self) -> tuple[list[str], list[str]]:
        return ["data"], [self.score_field]

    def setup(self, worker_metadata: WorkerMetadata | None = None) -> None:
        if self.name in ["lang_id", "fasttext_quality_filter"]:
            self.score_fn.model = fasttext.load_model(self.score_fn._model_path)  # noqa: SLF001

    def process(self, batch: DocumentBatch) -> DocumentBatch | None:
        """
        Applies the scoring to a dataset

        Args:
            batch (DocumentBatch): The batch to apply the module to

        Returns:
            DocumentBatch: A batch with the new score

        """
        # TODO: is_batched

        if isinstance(self.score_fn, DocumentFilter):
            self.score_fn = self.score_fn.score_document

        df = batch.to_pandas()
        df[self.score_field] = df[self.text_field].apply(self.score_fn)

        # Create output batch
        output_batch = DocumentBatch(
            task_id=f"{batch.task_id}_{self.name}",
            dataset_name=batch.dataset_name,
            data=df,
        )

        return output_batch


@dataclass
class Filter(ProcessingStage[DocumentBatch, DocumentBatch]):
    """
    The module responsible for filtering records based on a metadata field.
    It accepts an arbitrary filter function that accepts a metadata field and returns True if the field should be kept.
    It also accepts a DocumentFilter object, in which case the filter_fn will be the keep_document method of the DocumentFilter.
    Unlike ScoreFilter, it does not compute the metadata based on a document.
    It only filters using existing metadata.

    Args:
        filter_fn (Callable | DocumentFilter): A function that returns True if the document is to be kept or a DocumentFilter object,
            in which case the filter_fn will be the keep_document method of the DocumentFilter.
        filter_field (str): The field(s) to be passed into the filter function.
        invert (bool): Whether to invert the filter condition.

    """

    filter_fn: Callable | DocumentFilter
    filter_field: str
    invert: bool = False

    @property
    def name(self) -> str:
        return self.filter_fn.name if isinstance(self.filter_fn, DocumentFilter) else "filter_fn"

    def inputs(self) -> tuple[list[str], list[str]]:
        return ["data"], [self.filter_field]

    def outputs(self) -> tuple[list[str], list[str]]:
        return ["data"], []

    def compute_filter_mask(self, df: pd.DataFrame) -> pd.Series:
        """Compute the bool mask to filter the dataset.

        Args:
            df (pd.DataFrame): The dataset to compute filter mask on.

        Returns:
            Series: A mask corresponding to each data instance indicating whether it will be retained.

        """
        # TODO: is_batched

        if isinstance(self.filter_fn, DocumentFilter):
            self.filter_fn = self.filter_fn.keep_document

        bool_mask = df[self.filter_field].apply(self.filter_fn)

        if self.invert:
            bool_mask = ~bool_mask

        return bool_mask

    def process(self, batch: DocumentBatch) -> DocumentBatch | None:
        """
        Applies the filtering to a dataset

        Args:
            batch (DocumentBatch): The batch to apply the module to

        Returns:
            DocumentBatch: A batch with entries removed according to the filter

        """
        df = batch.to_pandas()
        bool_mask = self.compute_filter_mask(df)
        result_df = df[bool_mask]

        if len(result_df) == 0:
            print(f"All documents filtered out for batch {batch.task_id}")
            return None

        # Create output batch
        output_batch = DocumentBatch(
            task_id=f"{batch.task_id}_{self.name}",
            dataset_name=batch.dataset_name,
            data=result_df,
        )

        return output_batch


@dataclass
class ScoreFilter(ProcessingStage[DocumentBatch, DocumentBatch]):
    """
    The module responsible for applying a filter to all documents in a dataset.
    It accepts an arbitrary DocumentFilter and first computes the score for a document.
    Then, determines whether to keep the document based on the criteria in the DocumentFilter.

    The filter can be applied to any field in the dataset, and the score can be logged for later.
    Also, the filter can be inverted such that "rejected" documents are kept.

    Args:
        filter_obj (DocumentFilter): The score function that takes in a document string and outputs a score for the document.
        text_field (str): The field the documents will be read from.
        score_field: The field to which the scores will be written. If None, scores will be immediately discarded after use.
        score_type (Union[type, str]): The datatype of the score that will be made for each document.
        invert (bool): If True, will keep all documents that are normally discarded.

    """

    filter_obj: DocumentFilter
    text_field: str = "text"
    score_field: str | None = None
    score_type: type | str | None = None
    invert: bool = False

    @property
    def name(self) -> str:
        return self.filter_obj.name

    def inputs(self) -> tuple[list[str], list[str]]:
        return ["data"], [self.text_field]

    def outputs(self) -> tuple[list[str], list[str]]:
        return ["data"], [self.score_field] if self.score_field else []

    def setup(self, worker_metadata: WorkerMetadata | None = None) -> None:
        # TODO: Could use setup_on_node to verify that the model is downloaded
        if self.name in ["lang_id", "fasttext_quality_filter"]:
            self.filter_obj.model = fasttext.load_model(self.filter_obj._model_path)  # noqa: SLF001

    def compute_filter_mask(self, df: pd.DataFrame) -> pd.Series:
        """Compute the bool mask to filter the dataset.

        Args:
            df (pd.DataFrame): The dataset to compute filter mask on.

        Returns:
            Series: A mask corresponding to each data instance indicating whether it will be retained.

        """
        # TODO: is_batched

        scores = df[self.text_field].apply(self.filter_obj.score_document)

        if self.score_field is not None:
            df[self.score_field] = scores

        bool_mask = scores.apply(self.filter_obj.keep_document)

        if self.invert:
            bool_mask = ~bool_mask

        return bool_mask

    def process(self, batch: DocumentBatch) -> DocumentBatch | None:
        """
        Scores and filters all records in the dataset

        Args:
            batch (DocumentBatch): The batch to apply the module to

        Returns:
            DocumentBatch: A batch with the score and filter applied

        """
        df = batch.to_pandas()
        bool_mask = self.compute_filter_mask(df)
        result_df = df[bool_mask]

        if len(result_df) == 0:
            print(f"All documents filtered out for batch {batch.task_id}")
            return None

        # Create output batch
        output_batch = DocumentBatch(
            task_id=f"{batch.task_id}_{self.name}",
            dataset_name=batch.dataset_name,
            data=result_df,
        )

        return output_batch
