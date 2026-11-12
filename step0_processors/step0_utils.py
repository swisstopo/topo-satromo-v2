import os
import pandas as pd
import configuration as config

def write_asset_as_empty(collection, day_to_process, remark):
    print('Cutting asset create for {} / {}'.format(collection, day_to_process))
    print('Reason: {}'.format(remark))
    collection_name = os.path.basename(collection)

    # Do not append a row that is already in the CSV. The same date is run through
    # the processor again and again (check_s2_sr.py writes e.g. 'No candidate scene',
    # rerun.py takes that row as work and starts the processor, the processor ends
    # here with the same remark), which used to add an identical row every time.
    try:
        df_existing = pd.read_csv(config.EMPTY_ASSET_LIST)
        already_there = ((df_existing['collection'] == collection_name)
                         & (df_existing['date'] == day_to_process)
                         & (df_existing['remark'] == remark)).any()
    except (FileNotFoundError, pd.errors.EmptyDataError):
        already_there = False

    if already_there:
        print('Entry already in {} -- not written again'.format(config.EMPTY_ASSET_LIST))
        return

    df = pd.DataFrame([(collection_name, day_to_process, remark)])
    df.to_csv(config.EMPTY_ASSET_LIST, mode='a', header=False, index=False)
