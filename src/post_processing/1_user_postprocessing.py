import pandas as pd
import os
import re
from multiprocessing import Pool, cpu_count


def _filter_invalid_aliases(aliases_set):
    """
    Filters a frozenset of aliases, keeping only valid ones.
    A valid alias is a string that meets the specified length criteria,
    which depends on whether it contains an "@" symbol.
    This is critical to prevent a single invalid alias from deleting an entire user.
    """
    aliases_set = frozenset(aliases_set)

    valid_aliases = set()
    for alias in aliases_set:
        if isinstance(alias, str):
            # Apply the new length logic
            if "@" in alias:
                if len(alias) <= 60:
                    valid_aliases.add(alias)
            else:
                if 5 < len(alias) <= 35:
                    valid_aliases.add(alias)

    return frozenset(valid_aliases)

def _update_df(user_df, matches_dict):
    """
    Updates the DataFrame by merging aliases and dropping rows based on a matches dictionary.
    Returns the modified DataFrame.
    """
    if not matches_dict:
        return user_df

    child_ids_to_drop = list(matches_dict.keys())

    for child_id, parent_id in matches_dict.items():
        child_aliases = user_df.loc[child_id, "aliases"]
        parent_aliases = user_df.loc[parent_id, "aliases"]

        child_aliases = frozenset(child_aliases)
        parent_aliases = frozenset(parent_aliases)

        user_df.at[parent_id, "aliases"] = parent_aliases | child_aliases

    return user_df.drop(child_ids_to_drop)


def _match_worker(chunk, name_df):
    """
    Worker function for multiprocessing. Finds regex matches in a chunk of data.
    """
    matches_dict = {}

    # Pre-compile patterns from name_df outside the inner loop
    parent_patterns = []
    for parent_id, parent_row in name_df.iterrows():
        first_name = parent_row["first_name"]
        last_name = parent_row["last_name"]

        # Pattern 1: <first_initial><last_name>
        pattern_1 = re.compile(f"^{first_name[0]}{last_name}$")
        # Pattern 2: <first_name> then <last_name> anywhere
        pattern_2 = re.compile(f"{first_name}.*{last_name}")

        parent_patterns.append((parent_id, [pattern_1, pattern_2]))

    for child_id, row in chunk.iterrows():
        child_aliases = row["aliases"]
        for alias in child_aliases:
            for parent_id, patterns in parent_patterns:
                if patterns[0].search(alias) or patterns[1].search(alias):
                    if child_id != parent_id:
                        matches_dict[child_id] = parent_id
                        break
            if child_id in matches_dict:
                break
    return matches_dict


def run(paths: dict):
    USER_TABLE_OUTPUT_PATH = paths["user_table"]
    USER_MAP_TABLE_OUTPUT_PATH = paths["user_map_table"]
    USER_TABLE_UPDATED_OUTPUT_PATH = paths["user_table_updated"]
    TO_DELETE_OUTPUT_PATH = paths["to_delete_table"]

    if os.path.exists(USER_MAP_TABLE_OUTPUT_PATH):
        print("User map path already exists, skipping...")
        return

    user_df = pd.read_parquet(USER_TABLE_OUTPUT_PATH).set_index("user_id")

    # Stage 1: Initial exact alias matching (single-threaded)

    user_df["aliases"] = user_df["aliases"].apply(_filter_invalid_aliases)

    # Identify users for deletion: those with no name and no valid aliases
    is_name_empty = user_df["first_name"].str.strip().eq("") & user_df["last_name"].str.strip().eq("")
    has_no_aliases = user_df["aliases"].apply(lambda x: not x)

    to_delete = user_df[is_name_empty & has_no_aliases].index.to_list()

    if to_delete:
        print(
            f"Identified {len(to_delete)} users with no name and no valid aliases. These will be written to the to_delete table.")

    # Drop the identified users before proceeding with matching
    user_df = user_df.drop(to_delete)

    has_name_mask = (user_df["first_name"] != "") & (user_df["last_name"] != "")
    name_df = user_df[has_name_mask]

    alias_to_id_map = {}
    for user_id, row in user_df.iterrows():
        for alias in row["aliases"]:
            alias_to_id_map[alias] = user_id

    matches_dict_1 = {}
    for user_id, row in name_df.iterrows():
        if user_id in matches_dict_1:
            continue
        for gen_alias in row["generated_aliases"]:
            if gen_alias in alias_to_id_map:
                child_id = alias_to_id_map[gen_alias]
                if user_id != child_id:
                    matches_dict_1[child_id] = user_id

    print(f"Stage 1: Found {len(matches_dict_1)} initial matches.")

    # Stage 2: Update user_df in memory with the first set of matches
    user_df = _update_df(user_df, matches_dict_1)

    # Stage 3: Regex-based matching (using multiprocessing)
    has_name_mask_updated = (user_df["first_name"] != "") & (user_df["last_name"] != "")
    name_df_updated = user_df[has_name_mask_updated]
    no_name_df_updated = user_df[~has_name_mask_updated]

    print(f"Stage 2: Starting multiprocessing for {len(no_name_df_updated)} users...")

    # Split no_name_df into chunks for parallel processing
    num_cores = cpu_count()
    chunk_size = len(no_name_df_updated) // num_cores + 1
    chunks = [no_name_df_updated[i:i + chunk_size] for i in range(0, len(no_name_df_updated), chunk_size)]

    with Pool(processes=num_cores) as pool:
        # Map the worker function to each chunk
        results = pool.starmap(_match_worker, [(chunk, name_df_updated) for chunk in chunks])

    matches_dict_2 = {}
    for result_dict in results:
        matches_dict_2.update(result_dict)

    print(f"Stage 3: Found {len(matches_dict_2)} regex matches.")

    # Stage 4: Final update to users and write final outputs
    user_df = _update_df(user_df, matches_dict_2)

    # Combine both match dictionaries for the final user_map table
    matches_dict_1.update(matches_dict_2)

    # Outputting the combined user map to parquet, as this is the "to delete" table
    df_to_delete = pd.DataFrame.from_dict(matches_dict_1, orient="index", columns=["parent_id"])
    df_to_delete.index.name = "child_id"
    df_to_delete = df_to_delete.reset_index()
    df_to_delete.to_parquet(USER_MAP_TABLE_OUTPUT_PATH, engine='pyarrow', index=False)

    # Append the list of users with no name/alias to the to_delete table
    to_delete.extend(df_to_delete["child_id"].to_list())

    final_to_delete_df = pd.DataFrame(to_delete, columns=["user_id"])
    final_to_delete_df.to_parquet(TO_DELETE_OUTPUT_PATH, engine="pyarrow", index=False)

    # Convert the 'aliases' column to a list before writing to Parquet to avoid ArrowInvalid error
    user_df["aliases"] = user_df["aliases"].apply(list)

    user_df = user_df.reset_index()
    user_df.to_parquet(USER_TABLE_UPDATED_OUTPUT_PATH, engine="pyarrow", index=False)
    print("Updated user table and user map created successfully.")


