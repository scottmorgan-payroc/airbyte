#
# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
#

import shutil
from pathlib import Path
from typing import Any

import git  # type: ignore
from anyio import Semaphore  # type: ignore
from connector_ops.utils import ConnectorLanguage  # type: ignore
from pipelines.airbyte_ci.connectors.consts import CONNECTOR_TEST_STEP_ID
from pipelines.airbyte_ci.connectors.context import ConnectorContext
from pipelines.airbyte_ci.connectors.migrate_to_manifest_only.utils import (
    get_latest_base_image,
    readme_for_connector,
    revert_connector_directory,
)
from pipelines.airbyte_ci.connectors.reports import ConnectorReport
from pipelines.helpers.execution.run_steps import STEP_TREE, StepToRun, run_steps
from pipelines.models.steps import Step, StepResult, StepStatus
from ruamel.yaml import YAML  # type: ignore

## GLOBAL VARIABLES ##

VALID_SOURCE_FILES = ["manifest.yaml", "run.py", "__init__.py", "source.py"]
FILES_TO_LEAVE = ["manifest.yaml", "metadata.yaml", "icon.svg", "integration_tests", "acceptance-test-config.yml"]


# Initialize YAML handler with desired output style
yaml = YAML()
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.preserve_quotes = True


## STEPS ##

class CheckIsManifestMigrationCandidate(Step):
    """
    Pipeline step to check if the connector is a candidate for migration to to manifest-only.
    """

    context: ConnectorContext
    title: str = "Validate Manifest Migration Candidate"
    airbyte_repo: git.Repo = git.Repo(search_parent_directories=True)
    invalid_files: list = []

    async def _run(self) -> StepResult:

        ## 1. Confirm the connector is low-code and not already manifest-only
        if self.context.connector.language != ConnectorLanguage.LOW_CODE:
            return StepResult(
                step=self,
                status=StepStatus.SKIPPED,
                stderr=f"The connector is not a low-code connector.",
            )

        if self.context.connector.language == ConnectorLanguage.MANIFEST_ONLY:
            return StepResult(
                step=self,
                status=StepStatus.SKIPPED,
                stderr="The connector is already in manifest-only format.",
            )

        ## 2. Detect invalid python files in the connector's source directory
        connector_source_code_dir = self.context.connector.code_directory / self.context.connector.technical_name.replace("-", "_")
        for file in connector_source_code_dir.iterdir():
            if file.name not in VALID_SOURCE_FILES:
                self.invalid_files.append(file.name)
        if self.invalid_files:
            return StepResult(
                step=self,
                status=StepStatus.SKIPPED,
                stdout=f"The connector has unrecognized source files: {self.invalid_files}",
            )

        ## 3. Detect connector class name to make sure it's inherited from source declarative manifest
        # and does not override the `streams` method
        connector_source_py = (connector_source_code_dir / "source.py").read_text()

        if "YamlDeclarativeSource" not in connector_source_py:
            return StepResult(
                step=self,
                status=StepStatus.SKIPPED,
                stdout="The connector does not use the YamlDeclarativeSource class.",
            )

        if "def streams" in connector_source_py:
            return StepResult(
                step=self,
                status=StepStatus.SKIPPED,
                stdout="The connector overrides the streams method.",
            )

        return StepResult(
            step=self, status=StepStatus.SUCCESS, stdout=f"{self.context.connector.technical_name} is a valid candidate for migration."
        )


class StripConnector(Step):
    """
    Step to strip a low-code connector to manifest-only files.
    """

    context: ConnectorContext
    title = "Strip Unnecessary Files"

    def _delete_file(self, file: Path) -> None:
        self.logger.info(f"Deleting {file.name}")
        try:
            if file.is_dir():
                shutil.rmtree(file)
            else:
                file.unlink()
        except Exception as e:
            raise ValueError(f"Failed to delete {file.name}: {e}")

    async def _run(self) -> StepResult:

        ## 1. Move manifest.yaml to the root level of the directory
        connector_source_code_dir = self.context.connector.code_directory / self.context.connector.technical_name.replace("-", "_")
        self.logger.info(f"Moving manifest to the root level of the directory")

        manifest_file = connector_source_code_dir / "manifest.yaml"
        root_manifest_file = manifest_file.rename(self.context.connector.code_directory / "manifest.yaml")

        if root_manifest_file not in self.context.connector.code_directory.iterdir():
            return StepResult(
                step=self, status=StepStatus.FAILURE, stdout="Failed to move manifest.yaml to the root level of the directory."
            )

        ## 2. Delete all non-essential files
        try:
            for file in self.context.connector.code_directory.iterdir():
                if file.name in FILES_TO_LEAVE:
                    continue  # Preserve the allowed files
                elif file.name == "unit_tests":
                    # Preserve any integration tests, delete the rest
                    for unit_test_file in file.iterdir():
                        if unit_test_file.name in {"integration", "integrations"}:
                            continue
                        else:
                            self._delete_file(unit_test_file)
                # Delete everything else in root folder
                else:
                    self._delete_file(file)
        except Exception as e:
            return StepResult(step=self, status=StepStatus.FAILURE, stdout=f"Failed to delete files: {e}")

        ## 3. Verify that only allowed files remain
        for file in self.context.connector.code_directory.iterdir():
            if file.name not in FILES_TO_LEAVE and file.name != "unit_tests":
                return StepResult(step=self, status=StepStatus.FAILURE, stdout=f"Failed to delete {file.name}")

        return StepResult(step=self, status=StepStatus.SUCCESS, stdout="The connector has been successfully stripped.")


class UpdateManifestOnlyFiles(Step):
    """
    Step to update metadata, acceptance-test-config and readme to comply with manifest-only requirements.
    """

    context: ConnectorContext
    title = "Update Connector Metadata"

    async def _run(self) -> StepResult:

        ## 1. Update the acceptance test config to point to the right spec path
        acceptance_test_config = self.context.connector.code_directory / "acceptance-test-config.yml"
        try:
            with open(acceptance_test_config, "r") as file:
                acceptance_test_config_data = yaml.load(file)

                # Handle legacy acceptance test config:
                if "acceptance_tests" in acceptance_test_config_data:
                    acceptance_test_config_data["acceptance_tests"]["spec"]["tests"][0]["spec_path"] = "manifest.yaml"
                else:
                    acceptance_test_config_data["tests"]["spec"][0]["spec_path"] = "manifest.yaml"

            with open(acceptance_test_config, "w") as file:
                yaml.dump(acceptance_test_config_data, file)
        except Exception as e:
            return StepResult(step=self, status=StepStatus.FAILURE, stdout=f"Failed to update acceptance-test-config.yml: {e}")

        ## 2. Update the connector's metadata
        metadata_file = self.context.connector.code_directory / "metadata.yaml"
        # Backup the original file before modifying
        backup_metadata_file = self.context.connector.code_directory / "metadata.yaml.bak"
        shutil.copy(metadata_file, backup_metadata_file)
        self.logger.info(f"Updating metadata file")

        try:
            with open(metadata_file, "r") as file:
                metadata = yaml.load(file)
                if metadata and "data" in metadata:
                    # Update the metadata tab
                    tags = metadata["data"]["tags"]
                    tags.append("language:manifest-only")

                    # Update the base image
                    latest_base_image = get_latest_base_image("airbyte/source-declarative-manifest")
                    connector_base_image = metadata["data"]["connectorBuildOptions"]
                    connector_base_image["baseImage"] = latest_base_image

                    # Write the changes to metadata.yaml
                    with open(metadata_file, "w") as file:
                        yaml.dump(metadata, file)
                else:
                    return StepResult(step=self, status=StepStatus.FAILURE, stdout="Failed to read metadata.yaml content.")

        except Exception as e:
            # Restore the original file and return the report
            shutil.copy(backup_metadata_file, metadata_file)
            backup_metadata_file.unlink()
            return StepResult(step=self, status=StepStatus.FAILURE, stdout=f"Failed to update metadata.yaml: {e}")

        # delete the backup metadata file once done
        backup_metadata_file.unlink()

        ## 3. Update the connector's README
        self.logger.info(f"Updating README file")

        readme = readme_for_connector(self.context.connector.technical_name)
        with open(self.context.connector.code_directory / "README.md", "w") as file:
            file.write(readme)

        return StepResult(step=self, status=StepStatus.SUCCESS, stdout="The connector has been successfully migrated to manifest-only.")


## MAIN FUNCTION ##
async def run_connectors_manifest_only_pipeline(context: ConnectorContext, semaphore: "Semaphore", *args: Any) -> ConnectorReport:

    steps_to_run: STEP_TREE = []
    steps_to_run.append([StepToRun(id=CONNECTOR_TEST_STEP_ID.MANIFEST_ONLY_CHECK, step=CheckIsManifestMigrationCandidate(context))])

    steps_to_run.append(
        [
            StepToRun(
                id=CONNECTOR_TEST_STEP_ID.MANIFEST_ONLY_STRIP,
                step=StripConnector(context),
                depends_on=[CONNECTOR_TEST_STEP_ID.MANIFEST_ONLY_CHECK],
            )
        ]
    )

    steps_to_run.append(
        [
            StepToRun(
                id=CONNECTOR_TEST_STEP_ID.MANIFEST_ONLY_UPDATE,
                step=UpdateManifestOnlyFiles(context),
                depends_on=[CONNECTOR_TEST_STEP_ID.MANIFEST_ONLY_STRIP],
            )
        ]
    )

    async with semaphore:
        async with context:
            result_dict = await run_steps(
                runnables=steps_to_run,
                options=context.run_step_options,
            )
            results = list(result_dict.values())
            # If the pipeline failed, restore the connector directory to revert any changes
            if any(result.status == StepStatus.FAILURE for result in results):
                context.logger.error("The pipeline failed. Restoring the connector directory.")
                revert_connector_directory(context.connector.code_directory)

            report = ConnectorReport(context, steps_results=results, name="STRIP MIGRATION RESULTS")
            context.report = report

    return report
