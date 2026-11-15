import random
import re
import shutil
from pathlib import Path

import pandas as pd
from jinja2 import Environment, StrictUndefined
from rdagent.components.coder.factor_coder.config import FACTOR_COSTEER_SETTINGS
from rdagent.utils.env import QTDockerEnv


def generate_data_folder_from_qlib():
    template_path = Path(__file__).parent / "factor_data_template"
    qtde = QTDockerEnv()
    qtde.prepare()

    # Run the Qlib backtest
    execute_log = qtde.check_output(
        local_path=str(template_path),
        entry="python generate.py",
    )

    assert (Path(__file__).parent / "factor_data_template" / "daily_pv_all.h5").exists(), (
        "daily_pv_all.h5 is not generated. It means rdagent/scenarios/qlib/experiment/factor_data_template/generate.py is not executed correctly. Please check the log: \n"
        + execute_log
    )
    assert (Path(__file__).parent / "factor_data_template" / "daily_pv_debug.h5").exists(), (
        "daily_pv_debug.h5 is not generated. It means rdagent/scenarios/qlib/experiment/factor_data_template/generate.py is not executed correctly. Please check the log: \n"
        + execute_log
    )

    Path(FACTOR_COSTEER_SETTINGS.data_folder).mkdir(parents=True, exist_ok=True)
    shutil.copy(
        Path(__file__).parent / "factor_data_template" / "daily_pv_all.h5",
        Path(FACTOR_COSTEER_SETTINGS.data_folder) / "daily_pv.h5",
    )
    shutil.copy(
        Path(__file__).parent / "factor_data_template" / "README.md",
        Path(FACTOR_COSTEER_SETTINGS.data_folder) / "README.md",
    )

    Path(FACTOR_COSTEER_SETTINGS.data_folder_debug).mkdir(parents=True, exist_ok=True)
    shutil.copy(
        Path(__file__).parent / "factor_data_template" / "daily_pv_debug.h5",
        Path(FACTOR_COSTEER_SETTINGS.data_folder_debug) / "daily_pv.h5",
    )
    shutil.copy(
        Path(__file__).parent / "factor_data_template" / "README.md",
        Path(FACTOR_COSTEER_SETTINGS.data_folder_debug) / "README.md",
    )


def get_file_desc(p: Path, variable_list=[]) -> str:
    """
    Get the description of a file based on its type.

    Parameters
    ----------
    p : Path
        The path of the file.

    Returns
    -------
    str
        The description of the file.
    """
    p = Path(p)

    JJ_TPL = Environment(undefined=StrictUndefined).from_string("""
# {{file_name}}

## File Type
{{type_desc}}

## Content Overview
{{content}}
""")

    if p.name.endswith(".h5"):
        df = pd.read_hdf(p)
        pd.set_option("display.max_columns", None)
        pd.set_option("display.max_rows", None)
        pd.set_option("display.max_colwidth", None)

        df_info = "### Data Structure\n"
        df_info += (
            f"- Index: MultiIndex with levels {df.index.names}\n"
            if isinstance(df.index, pd.MultiIndex)
            else f"- Index: {df.index.name}\n"
        )

        df_info += "\n### Columns\n"
        columns = df.dtypes.to_dict()
        grouped_columns = {}

        for col in columns:
            if col.startswith("$"):
                prefix = col.split("_")[0] if "_" in col else col
                grouped_columns.setdefault(prefix, []).append(col)
            else:
                grouped_columns.setdefault("other", []).append(col)

        if variable_list:
            df_info += "#### Relevant Columns:\n"
            relevant_line = ", ".join(f"{col}: {columns[col]}" for col in variable_list if col in columns)
            df_info += relevant_line + "\n"
        else:
            df_info += "#### All Columns:\n"
            grouped_items = list(grouped_columns.items())
            random.shuffle(grouped_items)
            for prefix, cols in grouped_items:
                header = "Other Columns" if prefix == "other" else f"{prefix} Related Columns"
                df_info += f"\n#### {header}:\n"
                random.shuffle(cols)
                line = ", ".join(f"{col}: {columns[col]}" for col in cols)
                df_info += line + "\n"

        if "REPORT_PERIOD" in df.columns:
            one_instrument = df.index.get_level_values("instrument")[0]
            df_on_one_instrument = df.loc[pd.IndexSlice[:, one_instrument], ["REPORT_PERIOD"]]
            df_info += "\n### Sample Data\n"
            df_info += f"Showing data for instrument {one_instrument}:\n"
            df_info += str(df_on_one_instrument.head(5))

        return JJ_TPL.render(
            file_name=p.name,
            type_desc="HDF5 Data File",
            content=df_info,
        )

    elif p.name.endswith(".md"):
        with open(p) as f:
            content = f.read()
            return JJ_TPL.render(
                file_name=p.name,
                type_desc="Markdown Documentation",
                content=content,
            )

    else:
        raise NotImplementedError(
            f"file type {p.name} is not supported. Please implement its description function.",
        )


def get_data_folder_intro(fname_reg: str = ".*", flags=0, variable_mapping=None) -> str:
    """
    Describe explicitly provided factor data folders for the scenario prompt.

    Factor data is never downloaded or generated implicitly. Provide compatible
    prebuilt HDF5 source data in both configured folders.

    Directly get the info of the data folder.
    It is for preparing prompting message.

    Parameters
    ----------
    fname_reg : str
        a regular expression to filter the file name.

    flags: str
        flags for re.match

    Returns
    -------
        str
            The description of the data folder.
    """

    source_folders = {
        "FACTOR_COSTEER_DATA_FOLDER": Path(FACTOR_COSTEER_SETTINGS.data_folder),
        "FACTOR_COSTEER_DATA_FOLDER_DEBUG": Path(FACTOR_COSTEER_SETTINGS.data_folder_debug),
    }
    missing = [f"{env_name}={path}" for env_name, path in source_folders.items() if not path.is_dir()]
    if missing:
        raise FileNotFoundError(
            "Qlib factor source data is not configured. Provide prebuilt HDF5 data in both folders "
            "(" + ", ".join(missing) + "). No data is downloaded or generated automatically."
        )

    debug_files = [p for p in source_folders["FACTOR_COSTEER_DATA_FOLDER_DEBUG"].iterdir() if p.is_file()]
    source_files = [p for p in source_folders["FACTOR_COSTEER_DATA_FOLDER"].iterdir() if p.is_file()]
    has_source_hdf = any(p.suffix.lower() == ".h5" for p in source_files)
    has_debug_hdf = any(p.suffix.lower() == ".h5" for p in debug_files)
    if not has_source_hdf or not has_debug_hdf:
        raise FileNotFoundError(
            "Qlib factor source data folders must both contain prebuilt .h5 files. Configure "
            "FACTOR_COSTEER_DATA_FOLDER and FACTOR_COSTEER_DATA_FOLDER_DEBUG with HDF5 data."
        )

    content_l = []
    for p in debug_files:
        if re.match(fname_reg, p.name, flags) is not None:
            if variable_mapping:
                content_l.append(get_file_desc(p, variable_mapping.get(p.stem, [])))
            else:
                content_l.append(get_file_desc(p))
    return "\n----------------- file splitter -------------\n".join(content_l)
