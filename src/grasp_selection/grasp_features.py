"""
Main file for extracting grasp features.

Author: Brian Hou
"""
import argparse
import logging
import pickle as pkl
import os
import random
import string
import time

import IPython
import matplotlib.pyplot as plt
import numpy as np
import scipy.stats

import antipodal_grasp_sampler as ags
import database as db
import discrete_adaptive_samplers as das
import experiment_config as ec
import feature_functions as ff
import grasp_sampler as gs
import json_serialization as jsons
import kernels
import models
import objectives
import pfc
import pr2_grasp_checker as pgc
import termination_conditions as tc

def extract_features(obj, dest, feature_dest, config):
    # sample grasps
    sample_start = time.clock()
    if config['grasp_sampler'] == 'antipodal':
        logging.info('Using antipodal grasp sampling')
        sampler = ags.AntipodalGraspSampler(config)
        grasps = sampler.generate_grasps(
            obj, check_collisions=config['check_collisions'])

        # pad with gaussian grasps
        num_grasps = len(grasps)
        min_num_grasps = config['min_num_grasps']
        if num_grasps < min_num_grasps:
            target_num_grasps = min_num_grasps - num_grasps
            gaussian_sampler = gs.GaussianGraspSampler(config)
            gaussian_grasps = gaussian_sampler.generate_grasps(
                obj, target_num_grasps=target_num_grasps, check_collisions=config['check_collisions'])
            grasps.extend(gaussian_grasps)
    else:
        logging.info('Using Gaussian grasp sampling')
        sampler = gs.GaussianGraspSampler(config)
        grasps = sampler.generate_grasps(
            obj, check_collisions=config['check_collisions'])

    sample_end = time.clock()
    sample_duration = sample_end - sample_start
    logging.info('Grasp candidate generation took %f sec' %(sample_duration))

    if not grasps or len(grasps) == 0:
        logging.info('Skipping %s' %(obj.key))
        return

    # compute all features
    feature_start = time.clock()
    feature_extractor = ff.GraspableFeatureExtractor(obj, config)
    all_features = feature_extractor.compute_all_features(grasps)
    feature_end = time.clock()
    feature_duration = feature_end - feature_start
    logging.info('Feature extraction took %f sec' %(feature_duration))

    # generate pfc candidates
    graspable_rv = pfc.GraspableObjectGaussianPose(obj, config)
    f_rv = scipy.stats.norm(config['friction_coef'], config['sigma_mu'])
    candidates = []
    logging.info('%d grasps, %d valid features', len(grasps), len(all_features) - all_features.count(None))
    for grasp, features in zip(grasps, all_features):
        logging.info('Adding grasp %d candidate' %(len(candidates)))
        if features is None:
            logging.info('No features computed.')
            continue
        grasp_rv = pfc.ParallelJawGraspGaussian(grasp, config)
        pfc_rv = pfc.ForceClosureRV(grasp_rv, graspable_rv, f_rv, config)
        pfc_rv.set_features(features)
        candidates.append(pfc_rv)
    logging.info('%d candidates', len(candidates))

    # brute force with uniform allocation
    brute_force_iter = config['bandit_brute_force_iter']
    snapshot_rate = config['bandit_snapshot_rate']
    def phi(rv):
        return rv.features
    objective = objectives.RandomBinaryObjective()

    ua = das.UniformAllocationMean(objective, candidates)
    logging.info('Running uniform allocation for true pfc.')
    bandit_start = time.clock()
    ua_result = ua.solve(
        termination_condition=tc.MaxIterTerminationCondition(brute_force_iter),
        snapshot_rate=snapshot_rate)
    bandit_end = time.clock()
    bandit_duration = bandit_end - bandit_start
    logging.info('Uniform allocation (%d iters) took %f sec' %(brute_force_iter, bandit_duration))

    cand_grasps = [c.grasp for c in candidates]
    cand_features = [c.features_ for c in candidates]
    final_model = ua_result.models[-1]
    estimated_pfc = models.BetaBernoulliModel.beta_mean(
        final_model.alphas, final_model.betas)

    if len(cand_grasps) != len(estimated_pfc):
        logging.warning('Number of grasps does not match estimated pfc results.')
        IPython.embed()

    # write to file
    grasp_filename = os.path.join(dest, obj.key + '.json')
    with open(grasp_filename, 'w') as grasp_file:
        jsons.dump([g.to_json(quality=q, num_successes=a, num_failures=b) for g, q, a, b
                    in zip(cand_grasps, estimated_pfc, final_model.alphas, final_model.betas)],
                   grasp_file)

    # HACK to make paths relative
    features_as_json = [f.to_json(feature_dest) for f in cand_features]
    output_dest = os.path.split(dest)[0]
    for feature_as_json in features_as_json:
        feature_as_json = list(feature_as_json.values())[0]
        for wname in ('w1', 'w2'):
            wdata = feature_as_json[wname]
            for k, v in wdata.items():
                wdata[k] = os.path.relpath(v, output_dest) # relative to output_dest
    feature_filename = os.path.join(feature_dest, obj.key + '.json')
    with open(feature_filename, 'w') as feature_file:
        jsons.dump(features_as_json, feature_file)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('output_dest')
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.INFO)

    # read config file
    config = ec.ExperimentConfig(args.config)
    chunk = db.Chunk(config)

    # make output directory
    dest = os.path.join(args.output_dest, chunk.name)
    try:
        os.makedirs(dest)
    except os.error:
        pass

    feature_dest = os.path.join(args.output_dest, chunk.name, 'features')
    try:
        os.makedirs(feature_dest)
    except os.error:
        pass

    ff.FeatureExtractor.use_unity_weights = True
    for obj in chunk:
        logging.info('Extracting features for object {}'.format(obj.key))
        extract_features(obj, dest, feature_dest, config)
    logging.info('Features extracted.')
