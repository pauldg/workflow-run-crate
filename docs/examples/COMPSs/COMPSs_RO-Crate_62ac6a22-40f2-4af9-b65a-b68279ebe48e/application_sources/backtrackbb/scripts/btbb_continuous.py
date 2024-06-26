# -*- coding: utf8 -*-
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import sys
import time
import math
import numpy as np
from glob import glob
from obspy import Stream
from obspy import UTCDateTime
from collections import defaultdict
from multiprocessing import Pool

from pycompss.api.constraint import constraint
from pycompss.api.task import task
from pycompss.api.parameter import INOUT
from pycompss.api.parameter import FILE_OUT
from pycompss.api.parameter import COLLECTION_IN
from pycompss.api.parameter import COLLECTION_FILE_IN
from pycompss.api.parameter import DICTIONARY_IN
from pycompss.api.parameter import DIRECTORY_IN
from pycompss.api.parameter import Type, Depth
from pycompss.api.api import compss_wait_on
from pycompss.api.api import compss_barrier
from pycompss.api.api import compss_delete_object
from pycompss.streams.distro_stream import FileDistroStream


from backtrackbb.mod_setup import configure
from backtrackbb.read_traces import read_traces
from backtrackbb.read_traces_SDS import read_traces_SDS
from backtrackbb.init_filter import init_filter
from backtrackbb.read_grids import read_grid  # notice read_grid instead read_grids
from backtrackbb.summary_cf import summary_cf, empty_cf
from backtrackbb.mod_utils import read_locationTremor, read_locationEQ
from backtrackbb.plot import plt_SummaryOut
from backtrackbb.rec_memory import init_recursive_memory
from backtrackbb.mod_btbb import init_worker
from backtrackbb.mod_btbb import run_btbb
from backtrackbb.mod_btbb import run_btbb_no_fig

from backtrackbb.scripts.btbb_utils import parse_input_parameters


DEBUG = False

@task(returns=3, config=INOUT, basepath=DIRECTORY_IN)
def reading_data_SDS(config, basepath):
    st, t_bb, t_ee = __reading_data_SDS__(config, basepath)
    return st, t_bb, t_ee


@task(returns=3, config=INOUT, traces=COLLECTION_FILE_IN)
def reading_data(config, traces):
    st, t_bb, t_ee = __reading_data__(config, traces)
    return st, t_bb, t_ee


def __reading_data_SDS__(config, basepath):
    #---Reading data---------------------------------------------------------
    st, tbb_st = read_traces_SDS(config, basepath)
    #------------------------------------------------------------------------
    t_bb_conf = np.arange(config.start_t, config.end_t,
                          config.time_lag - config.t_overlap)
    #---- We'll only analyse time-windows for which we have data ------------
    t_bb = np.array([ttb for ttb in t_bb_conf if ttb in tbb_st])
    # Selecting the time windows that do not exceed the length of the data---
    t_ee = t_bb + config.time_lag
    data_length = st[0].stats.endtime - st[0].stats.starttime
    t_bb = t_ee[t_ee <= data_length] - config.time_lag
    #
    print('Number of time windows = ', len(t_bb))
    #------------------------------------------------------------------------
    return st, t_bb, t_ee


def __reading_data__(config, traces):
    #---Reading data---------------------------------------------------------
    #st = read_traces(config)  # old method - can cause issues with paths
    st = read_traces(config, traces)  # working method - but fails with gaps
    #------------------------------------------------------------------------
    t_bb = np.arange(config.start_t, config.end_t,
                     config.time_lag - config.t_overlap)
    # Selecting the time windows that do not exceed the length of the data---
    t_ee = t_bb + config.time_lag
    data_length = st[0].stats.endtime - st[0].stats.starttime
    t_bb = t_ee[t_ee <= data_length] - config.time_lag
    print('Number of time windows = ', len(t_bb))
    #------------------------------------------------------------------------
    return st, t_bb, t_ee


@task(returns=1, st=INOUT)
def remove_mean_and_trend(st):
    #---remove mean and trend------------------------------------------------
    st.detrend(type='constant')
    st.detrend(type='linear')
    #------------------------------------------------------------------------


@task(returns=1, p_outputs=COLLECTION_IN)
def filter_outputs(p_outputs):
    #----filter outputs------------------------------------------------------
    # new:
    p_outputs = np.concatenate(p_outputs).ravel().tolist()  # flatten
    triggers = list(filter(None, p_outputs))
    return triggers
    #------------------------------------------------------------------------


@task(file_out_triggers=FILE_OUT)
def write_outputs(file_out_triggers, triggers):
    #----------Outputs-------------------------------------------------------
    #writing output
    eventids = []
    with open(file_out_triggers, 'w') as f:
        for trigger in triggers:
            # check if eventid already exists
            while trigger.eventid in eventids:
                # increment the last letter by one
                evid = list(trigger.eventid)
                evid[-1] = chr(ord(evid[-1]) + 1)
                trigger.eventid = ''.join(evid)
            eventids.append(trigger.eventid)
            f.write(str(trigger) + '\n')
            # sort picks by station
            picks = sorted(trigger.picks, key=lambda x: x.station)
            for pick in picks:
                f.write(str(pick) + '\n')
    #------------------------------------------------------------------------


def main(all_config_paths, tw_x_task):

    start_time = time.time()

    # Process all configs
    all_configs = []
    for cfg in all_config_paths:
        # force a new cfg as sys.argv
        print("Processing config: " + str(cfg))
        sys.argv = ["btbb_continuous.py", cfg]

        config = configure()

        var_twin = config.varWin_stationPair
        print('use of var time window for location:', var_twin)

        #---Reading data---------------------------------------------------------
        basepath = config.data_dir
        if config.data_day:
            basepath = os.path.join(basepath, config.data_day)
            if config.data_hours:
                basepath = os.path.join(basepath, config.data_hours)

        if config.dataarchive_type == "SDS":
            st, t_bb, t_ee = reading_data_SDS(config, basepath)  # TASK
        else:
            traces = glob(os.path.join(basepath, '*'))
            st, t_bb, t_ee = reading_data(config, traces)  # TASK

        loc_infile = None
        location_jma = None
        if config.catalog_dir:
            if config.data_day:
                loc_infile = os.path.join(config.catalog_dir,
                                          config.data_day + config.tremor_file)
            location_jma = os.path.join(config.catalog_dir, config.eq_file)
        #------------------------------------------------------------------------

        #---remove mean and trend------------------------------------------------
        remove_mean_and_trend(st)  # TASK
        #------------------------------------------------------------------------

        #---init filtering parameters--------------------------------------------
        init_filter(config)  # TASK

        all_configs.append([config, st, t_bb])

    #--Read grids of theoretical travel-times--------------------------------
    # GRD_sta = defaultdict(dict)
    GRD_sta = {}
    coord_sta = {}
    first_station = None
    first_grid_type = None
    for grid_type in config.grid_type:
        print("grid_type: " + str(grid_type))
        for station in config.stations:
            print("station: " + str(station))
            # grid_bname = '.'.join(('*', grid_type, station, 'time.hdr'))
            grid_bname = '.'.join(('layer', grid_type, station, 'time.hdr'))
            grid_bname = os.path.join(config.grid_dir, grid_bname)
            print("grid_bname: " + str(grid_bname))
            grid_bname = glob(grid_bname)[0]
            GRD_sta[station] = {}
            GRD_sta[station][grid_type], coord_sta[station] = read_grid(grid_bname)  # TASK
            if first_station is None and first_grid_type is None:
                first_station = station
                first_grid_type = grid_type
    #------------------------------------------------------------------------

    # sync configs:
    # all_configs = compss_wait_on(all_configs)
    sync_all_configs = []
    for config, st, t_bb in all_configs:
        config = compss_wait_on(config)  # access later to some config parameters (e.g. recursive_memory)
        st = compss_wait_on(st)  # access later to inner element (e.g. st[0].stats to get the start time)
        t_bb = compss_wait_on(t_bb)  # required to calculate the number of blocks
        sync_all_configs.append([config, st, t_bb])
    all_configs = sync_all_configs

    #---Take the first grid as reference ------------------------------------
    # for x in GRD_sta.keys():
    #     for y in GRD_sta[x].keys():
    #         GRD_sta[x][y] = compss_wait_on(GRD_sta[x][y])
    # grid1 = list(list(GRD_sta.values())[0].values())[0]

    grid1 = GRD_sta[first_station][first_grid_type]
    # grid1 = compss_wait_on(grid1)
    # GRD_sta[first_station][first_grid_type] = grid1  # replace since it has been removed at sync.
    #------------------------------------------------------------------------

    # Next loop
    all_st_CF = []
    all_coord_eq = []
    all_rec_memory = []
    all_file_out_base = []
    for config, st, t_bb in all_configs:
        if config.recursive_memory:
            rec_memory = init_recursive_memory(config)  # TASK
            st_CF = empty_cf(config, st)  # TASK
            # if DEBUG:
            #     st_CF2 = summary_cf(config, st)  # TASK
        else:
            rec_memory = None
            st_CF = summary_cf(config, st)  # TASK
        all_st_CF.append(st_CF)
        all_rec_memory.append(rec_memory)
        #------------------------------------------------------------------------

        #----geographical coordinates of the eq's epicenter----------------------
        coord_eq = None
        if loc_infile:
            coord_eq = read_locationTremor(loc_infile, config)  # TASK
        all_coord_eq.append(coord_eq)
        coord_jma = None
        if location_jma:
            coord_jma = read_locationEQ(location_jma, config)  # TASK
        #------------------------------------------------------------------------
        print('starting BPmodule')

        #-----Create out_dir, if it doesn't exist--------------------------------
        if not os.path.exists(config.out_dir):  # TODO: move to top
            os.mkdir(config.out_dir)

        datestr = st[0].stats.starttime.strftime('%y%m%d%H')
        fq_str = '%s_%s' % (np.round(config.frequencies[0]),
                            np.round(config.frequencies[-1]))
        ch_str = str(config.channel)[1:-1].replace("'", "")
        file_out_base = '_'.join((
            datestr,
            str(len(config.frequencies)) + 'fq' + fq_str + 'hz',
            str(config.decay_const) + str(config.sampl_rate_cf) +
            str(config.smooth_lcc) + str(config.t_overlap),
            config.ch_function,
            ch_str,
            ''.join(config.wave_type),
            'trig' + str(config.trigger)
            ))
        all_file_out_base.append(file_out_base)
        #------------------------------------------------------------------------

    pos = 0
    for config, st, t_bb in all_configs:
        #---running program------------------------------------------------------
        print("Running config: " + str(pos))
        st_CF = all_st_CF[pos]
        coord_eq = all_coord_eq[pos]
        rec_memory = all_rec_memory[pos]
        file_out_base = all_file_out_base[pos]
        pos = pos + 1

        # p_outputs = []
        # for t_begin in t_bb:
        #
        #     fq_str = '%s_%s' % (str(np.round(config.frequencies[0])),
        #                         str(np.round(config.frequencies[-1])))
        #     datestr = st[0].stats.starttime.strftime('%y%m%d%H')
        #
        #     file_out_fig =\
        #         datestr + '_t' + '%06.1f' % (config.cut_start+t_begin) +\
        #         's_' + fq_str + '_fig.' + config.plot_format
        #     file_out_fig = os.path.join(config.out_dir, file_out_fig)
        #
        #     if config.plot_results is 'True':
        #         p_outputs.append(run_btbb(config,
        #                                   st,
        #                                   st_CF,
        #                                   t_begin,
        #                                   coord_eq,
        #                                   coord_sta,
        #                                   rec_memory,
        #                                   grid1,
        #                                   GRD_sta,
        #                                   file_out_fig))  # run_btbb TASK
        #     else:
        #         p_outputs.append(run_btbb_no_fig(config,
        #                                          st,
        #                                          st_CF,
        #                                          t_begin,
        #                                          coord_eq,
        #                                          coord_sta,
        #                                          rec_memory,
        #                                          grid1,
        #                                          GRD_sta,
        #                                          file_out_fig))  # run_btbb_no_fig  TASK
        # p_outputs = btbb_block(config,
        #                        t_bb
        #                        st,
        #                        st_CF,
        #                        coord_eq,
        #                        coord_sta,
        #                        rec_memory,
        #                        grid1,
        #                        GRD_sta)

        # number of blocks of time windows
        num_blocks = int(math.ceil(len(t_bb) / tw_x_task))
        print("st: %s" % str(st))
        print("t_bb: %s" % str(t_bb))
        print("len(t_bb): %s" % str(len(t_bb)))
        print("tw_x_task: %s" % str(tw_x_task))
        print("num_blocks: %s" % str(num_blocks))
        blocks = np.array_split(t_bb, num_blocks)

        p_outputs = []
        for block in blocks:
            # TODO: it is not openning internal threads because it has only one
            # time window... in order to exploit internal parallelism should
            # receive as many time windows as internal processes.
            # Submit as many time windows as config.ncpu!
            p_outputs.append(btbb_block(config,
                                        block,  # t_bb
                                        st,
                                        st_CF,
                                        coord_eq,
                                        coord_sta,
                                        rec_memory,
                                        grid1,
                                        GRD_sta))

        # """
        #----filter outputs------------------------------------------------------
        triggers = filter_outputs(p_outputs)  # TASK
        #------------------------------------------------------------------------

        #----------Outputs-------------------------------------------------------
        file_out_triggers = file_out_base + '_OUT2.dat'
        file_out_triggers = os.path.join(config.out_dir, file_out_triggers)
        write_outputs(file_out_triggers, triggers)  # TASK
        #------------------------------------------------------------------------

        #-plot summary output----------------------------------------------------
        file_out_fig = file_out_base + '_FIG2.' + config.plot_format
        file_out_fig = os.path.join(config.out_dir, file_out_fig)
        plt_SummaryOut(config, grid1, st_CF, st, coord_sta,
                       triggers, t_bb, datestr,
                       coord_eq, coord_jma, file_out_fig)  # TASK
        #------------------------------------------------------------------------
        # """

        # print("Deleting objects on iteration...")
        # compss_delete_object(config)
        # compss_delete_object(st)
        # compss_delete_object(st_CF)
        # compss_delete_object(coord_eq)
        # for k, v in coord_sta.items():
        #     # compss_delete_object(k)
        #     compss_delete_object(v)
        # # compss_delete_object(coord_sta)
        # compss_delete_object(rec_memory)

        # if DEBUG:
        #     import matplotlib.pyplot as plt
        #     st_CF = compss_wait_on(st_CF)
        #     st_CF2 = compss_wait_on(st_CF2)
        #     CF = st_CF.select(station=config.stations[0])[0]
        #     CF2 = st_CF2.select(station=config.stations[0])[0]
        #     fig = plt.figure()
        #     ax1 = fig.add_subplot(211)
        #     ax1.plot(CF, linewidth=2)
        #     ax1.plot(CF2[0:len(CF)])
        #     ax2 = fig.add_subplot(212, sharex=ax1)
        #     ax2.plot(CF - CF2[0:len(CF)])
        #     plt.show()

        #------------------------------------------------------------------------
    # compss_delete_object(t_begin)
    # print("Deleting final objects...")
    # for k, v in GRD_sta.items():
    #     for w, x in v.items():
    #         compss_delete_object(x)
    #         # compss_delete_object(w)
    #     compss_delete_object(v)
    #     # compss_delete_object(k)
    # # compss_delete_object(GRD_sta)


    compss_barrier()
    print("Elapsed time: " + str(time.time() - start_time))


@constraint(computing_units="${COMPUTING_UNITS}")
@task(returns=1, coord_sta={Type: DICTIONARY_IN, Depth: 1}, GRD_sta={Type: DICTIONARY_IN, Depth: 2})
def btbb_block(config,
               t_bb,
               st,
               st_CF,
               coord_eq,
               coord_sta,
               rec_memory,
               grid1,
               GRD_sta):
    arglist = []
    for t_begin in t_bb:

        fq_str = '%s_%s' % (str(np.round(config.frequencies[0])),
                            str(np.round(config.frequencies[-1])))
        datestr = st[0].stats.starttime.strftime('%y%m%d%H')

        file_out_fig =\
            datestr + '_t' + '%06.1f' % (config.cut_start+t_begin) +\
            's_' + fq_str + '_fig.' + config.plot_format
        file_out_fig = os.path.join(config.out_dir, file_out_fig)

        arglist.append((
            config,
            st,
            st_CF,
            t_begin,
            coord_eq,
            coord_sta,
            rec_memory,
            grid1,
            GRD_sta,
            file_out_fig
        ))

    p_outputs = []
    print('Running on %d thread%s' % (config.ncpu, 's' * (config.ncpu > 1)))
    if config.ncpu > 1 and not config.recursive_memory:
        # parallel execution
        pool = Pool(config.ncpu, init_worker)
        try:
            # we need to use map_async() (with a very long timeout)
            # due to a python bug
            # (http://stackoverflow.com/questions/1408356/
            #  keyboard-interrupts-with-pythons-multiprocessing-pool)
            if config.plot_results == 'True':
                p_outputs = pool.map_async(run_btbb, arglist).get(9999999)
            else:
                p_outputs = pool.map_async(run_btbb_no_fig, arglist).get(9999999)
        except KeyboardInterrupt as e:
            pool.terminate()
            pool.join()
            print('')
            print('Aborting.')
            raise Exception("ERROR using the multiprocessing pool: %s" % str(e))
        pool.close()
        pool.join()
    else:
        # serial execution
        # (but there might be a parallelization step inside run_btbb)
        if config.plot_results == 'True':
            p_outputs = list(map(run_btbb, arglist))
        else:
            p_outputs = list(map(run_btbb_no_fig, arglist))
    return p_outputs


def main_continuous():
    """
    Python code for running BTBB code over continous data by splitting into chunks of 1 hour
    """
    # # --- Input parameters
    # root_folder = '/gpfs/scratch/bsc19/bsc19234/BackTrackBB_stream/backtrackbb-master/scripts/'
    # # root_folder = '/gpfs/projects/pr1ejg00/BackTrackBB/backtrackbb-master/scripts/'
    # # -- Path to output folder --
    # outdir_base = root_folder + 'out_synthetic_continous_np/2222/01'
    # datadir_base = "/gpfs/scratch/pr1ejg00/pr1ejg19/data/2222/01" #root_folder + 'examples/data/data_Synthetic/2222/01'
    # # -- ID base file --
    # base_config_file = root_folder + 'examples/BT_SyntheticExample_MN_base.conf'
    #
    # studycase = "synthetic"
    # year = '2222'
    # month = '01'
    # days = np.arange(1, 2)  # (1, 4)  # (1, 8)
    # hours = np.arange(0, 12)
    # tw_x_task = 100  # 100 time windows per task
    #
    # studycase = 'vrancea'
    # year = '2017'
    # month = '01'
    # days = np.arange(1, 3)
    # hours = np.arange(0, 24)
    # tw_x_task = 100  # 100 time windows per task

    # Get input parameters
    args = parse_input_parameters()
    studycase = args.studycase
    datadir_base = args.datadir
    outdir_base = args.outdir
    base_config_file = args.config
    year = args.year
    month = args.month
    days = np.arange(args.day_start, args.day_end)
    hours = np.arange(args.hour_start, args.hour_end)
    tw_x_task = args.tw_x_task

    # Prepare all configurations for the given days and hours
    all_config_paths = []
    for day in days:
        day=str(day).zfill(2)
        if not os.path.exists(os.path.join(outdir_base, day)):
            os.mkdir(os.path.join(outdir_base, day))

        for hour in hours:
            hour=str(hour).zfill(2)
            outdir = os.path.join(outdir_base, day, hour)
            if not os.path.exists(outdir):
                os.mkdir(outdir)

            if studycase == 'synthetic':
                in_datadir = os.path.join(datadir_base, day, hour)
                config_name = ''.join(('BT_Synthetic',year,month,day,hour, '.conf'))

                config_path = os.path.join(outdir, config_name)

                cmd = 'cp ' + base_config_file + ' ' + config_path
                os.system(cmd)

                file = open(config_path, "a")
                file.write("out_dir = \'%s\'" % str(outdir))
                file.write("\n")
                file.write("data_dir = \'%s\'" % str(in_datadir))
                file.write("\n")
                file.close()
            elif studycase == 'vrancea':
                config_name = ''.join(('BT_vrancea',year,month,day,hour, '.conf'))
                config_path = os.path.join(outdir, config_name)

                cmd = 'cp ' + base_config_file + ' ' + config_path
                os.system(cmd)

                file = open(config_path, "a")
                file.write("out_dir = \'%s\'" % str(outdir))
                file.write("\n")

                starttime = UTCDateTime('-'.join((str(year), str(month), str(day), str(hour))))
                endtime = UTCDateTime('-'.join((str(year), str(month), str(day), str(hour))))+3600+10

                file = open(config_path, "a")
                file.write("start_time = \'%s\'" % str(starttime))
                file.write("\n")
                file.write("end_time = \'%s\'" % str(endtime))
                file.write("\n")
                file.close()
            else:
                raise Exception("Unsupported studycase.")

            all_config_paths.append(config_path)

    main(all_config_paths, tw_x_task)


if __name__ == '__main__':
    main_continuous()
