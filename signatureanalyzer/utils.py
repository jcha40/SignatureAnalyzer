import numpy as np
import pandas as pd
import sys
from tqdm import tqdm
import h5py
from sklearn.metrics.pairwise import cosine_similarity
import pkg_resources
import re
import itertools

from sys import stdout ### GET rid of later
from .context import context_composite, context96, context1536, context78, context83

from missingpy import KNNImputer, MissForest

COMPL = {"A":"T","T":"A","G":"C","C":"G"}

# ---------------------------------
# IOUtils
# ---------------------------------
def file_loader(x):
    if x.endswith('.csv'):
        return pd.read_csv(x, index_col=0)
    elif x.endswith('.parquet'):
        return pd.read_parquet(x)
    else:
        return pd.read_csv(x, sep='\t', index_col=0)

# ---------------------------------
# NMF Utils
# ---------------------------------
def split_negatives(x: pd.DataFrame, tag: str = '_n', axis: int = 0):
    """
    Split dataframe into positive and negative components.
    --------------------
    Args:
        * x: pd.DataFrame input matrix
        * tag: string that will be added to the end of the negative variable names
        * axis: which axis to create a positive dimension for
            NOTE: this is for 2D numpy arrays

    Returns:
        * pd.DataFrame with new positive transformed matrix.
    """
    x_neg = -1 * x.copy()
    x_neg = x_neg.where(x_neg > 0, 0)

    if axis:
        x.columns = x.columns.astype(str)
        x_neg.columns = [x+'_n' for x in x.columns]
    else:
        x.index = x.index.astype(str)
        x_neg.index = [x+'_n' for x in x.index]

    return pd.concat([x.where(x > 0, 0), x_neg], axis=axis)

def impute_values(df: pd.DataFrame, method: str = 'mean', **kwargs):
    """
    Impute missing values in DataFrame (np.nan or None).
    ------------------------
    Args:
        * df: pd.DataFrame of (samples x features)
        * method: string for what method of imputation to use
            ** 'mean': mean imputation
            ** 'knn': K-NN imputation (see missingpy.KNNImputer)
            ** 'rf': random forest imputation (see missingpy.MissForest)

    Returns:
        * pd.DataFrame: imputed values (samples x features)
    """
    assert method in ('mean','knn','rf'), '{} not yet implemented.'.format(method)

    if method=='mean':
        return df.fillna(df.mean(0))
    elif method=='knn':
        X = df.values
        imputer = KNNImputer(**kwargs)
        X_impute = imputer.fit_transform(X)
        return pd.DataFrame(X_impute, index=df.index, columns=df.columns)
    elif method=='rf':
        X = df.values
        imputer = MissForest(**kwargs)
        X_impute = imputer.fit_transform(X)
        return pd.DataFrame(X_impute, index=df.index, columns=df.columns)

def l2fc(df: pd.DataFrame, center: str = 'median', axis: int = 1):
    """
    Log2 Fold-Change Input Dataframe
    -------------------------
    Args:
        * df: pd.DataFrame
        * center: center-metrix to compute log-fold change over
            ** 'median'
            ** 'mean'
        * axis: int axis to compute median and LFC across (i.e. samples)

    Returns:
        * pd.Dataframe: log2FC-transformed pandas dataframe
    """
    X = df.values

    if center == 'median':
        X_mid = np.median(X, axis)
    elif center == 'mean':
        X_mid = np.mean(X, axis)

    if axis==1:
        return pd.DataFrame(np.log2(X) - np.log2(X_mid)[:,np.newaxis], index=df.index, columns=df.columns)
    else:
        return pd.DataFrame(np.log2(X) - np.log2(X_mid)[np.newaxis], index=df.index, columns=df.columns)

def compute_phi(mu: float, var: float, beta: float):
    """
    Compute Phi
    ------------------------
    Compute the Dispersion parameter.
    """
    return var / (mu ** (2-beta))

def transfer_weights(W: pd.DataFrame, H: pd.DataFrame, active_thresh:float = 1e-2):
    """
    Transfers weights from output of NMF.
    ------------------------
    Args:
        * W: input W matrix (K x n_features)
        * H: input H matrix (n_samples x K)
        * active_thresh: active threshold to consider a factor loading significant

    Returns:
        * W_final: normalized W matrix (K x n_features)
        * H_final: normalized H matrix (n_samples x K)
        * nsig: number of signatures found
    """
    W = W.copy()
    H = H.copy()

    # Active signatures
    nonzero_idx = (np.sum(H, axis=1) * np.sum(W, axis=0)) > active_thresh
    nsig = np.sum(nonzero_idx)
    sig_names = [str(i) for i in range(1,nsig+1)]

    # Raw matrices for active signatures
    W_active = pd.DataFrame(data=W[:, nonzero_idx])
    H_active = pd.DataFrame(data=H[nonzero_idx, :])

    W_weight = np.sum(W_active, axis=0)

    # Normalize W and transfer weight to H matrix
    W_final = W_active / W_weight
        
    H_final = W_weight[:, np.newaxis] * H_active

    return W_final, H_final, W_active, H_active, nsig, nonzero_idx

def select_signatures(W: pd.DataFrame, H: pd.DataFrame):
    """
    Scales NMF output by sample and feature totals to select Signatures.
    ------------------------
    Args:
        * W: input W matrix (K x n_features)
        * H: input H matrix (n_samples x K)

    Returns:
        * W: output W matrix with max_id, max, and max_norm columns
        * H: output H matrix with max_id, max, and max_norm columns
    """
    Wnorm = W.copy()
    Hnorm = H.copy()

    # Scale Matrix
    for j in range(W.shape[1]):
        Wnorm.iloc[:,j] *= H.sum(1).values[j]  # Multiple normalized signature contributions by their total mutation attribution to get total attribution per context
        Hnorm.iloc[j,:] *= W.sum(0).values[j]  # Multiply signature raw attributions by fraction of mutations per context
        
    # Normalize
    Wnorm = Wnorm.div(Wnorm.sum(1),axis=0)
    Hnorm = Hnorm.div(Hnorm.sum(0),axis=1)
    
    H = H.T
    Hnorm = Hnorm.T
    
    # Get Max Values
    H_max_id = H.idxmax(axis=1, skipna=True).astype('int')
    H['max'] = H.max(axis=1, skipna=True)
    H['max_id'] = H_max_id
    Hnorm['max_norm']=Hnorm.max(axis=1, skipna=True)

    W_max_id = W.idxmax(axis=1, skipna=True).astype('int')
    W['max'] = W.max(axis=1, skipna=True)
    W['max_id'] = W_max_id
    Wnorm['max_norm'] = Wnorm.max(axis=1, skipna=True)
    
    H['max_norm'] = Hnorm['max_norm']
    W['max_norm'] = Wnorm['max_norm']

    _rename = {x:'S'+x for x in list(H)[:-3]}
    H = H.rename(columns=_rename)
    W = W.rename(columns=_rename)

    return W,H

def select_markers(
    X: pd.DataFrame, \
    W: pd.DataFrame, \
    H: pd.DataFrame, \
    cut_norm: float = 0.5, \
    cut_diff: float = 1.0, \
    verbose: bool = False \
    ):
    """
    Marker selection from NMF.
    ------------------------
    Args:
        * X: Input X matrix (n_samples x n_features)
        * W: input W matrix (K x n_features) with max_id, max, and max_norm columns
        * H: input H matrix (n_samples x K) with max_id, max, and max_norm columns
        * cut_norm: minimum normalized signature strength
        * cut_diff: minimum difference between selected signature and other signatures

    Returns:
        * Pandas Dataframe of NMF markers
        * Pandas Dataframe of full W matrix
    """
    markers = list()
    full = list()

    pd.options.mode.chained_assignment = None

    for n in tqdm(np.unique(W['max_id']), desc='Clusters: ', disable=not verbose):
        if H[H['max_id']==n].shape[0] > 0:
            tmp = W[W['max_id']==n]
            tmp.loc[:,'mean_on'] = X.loc[np.array(tmp.index), H[H['max_id']==n].index].mean(axis=1)
            tmp.loc[:,'mean_off'] = X.loc[np.array(tmp.index), H[H['max_id']!=n].index].mean(axis=1)
            tmp.loc[:,'diff'] = tmp.loc[:,'mean_on'] - tmp.loc[:,'mean_off']

            tmp.sort_values('diff', ascending=False, inplace=True)
            full.append(tmp)
            markers.append(tmp[(tmp['diff'] > cut_diff) & (tmp['max_norm'] >= cut_norm)])

    nmf_markers = X.loc[pd.concat(markers).index,H.max_id.sort_values().index]
    nmf_markers.index.name = 'feat'

    return nmf_markers, pd.concat(full)

# ---------------------------------
# Mutational Signature Utils
# ---------------------------------
def load_reference_signatures(cosmic: str):
    """
    Load cosmic signatures.
    -------------------------
    Pre-processed Cosmic Mutational Signatures.
    """
    if cosmic == 'cosmic2':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/cosmic_v2/sa_cosmic2.tsv'), sep='\t').dropna(1)
        reference_index = "Somatic Mutation Type"
    elif cosmic == 'cosmic3':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/cosmic_v3/sa_cosmic3_sbs.tsv'), sep='\t').dropna(1)
        reference_index = "Somatic Mutation Type"
    elif cosmic == 'cosmic3_exome':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/cosmic_v3/sa_cosmic3_sbs_exome.tsv'), sep='\t').dropna(1)
        reference_index = "Somatic Mutation Type"
    elif cosmic == 'cosmic3_DBS':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/cosmic_v3/sa_cosmic3_dbs.tsv'), sep='\t').dropna(1)
        reference_index = "Somatic Mutation Type"
    elif cosmic == 'cosmic3_ID':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/cosmic_v3/sa_cosmic3_id.tsv'), sep='\t').dropna(1)
        reference_index = "Mutation Type"
    elif cosmic == 'pcawg_SBS':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/PCAWG/sa_PCAWG_sbs.tsv'), sep='\t').dropna(1)
        reference_index = 'Somatic Mutation Type'
    elif cosmic == 'pcawg_COMPOSITE':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/PCAWG/sa_PCAWG_composite.tsv'), sep='\t').dropna(1)
        reference_index = 'Somatic Mutation Type'
    elif cosmic == 'pcawg_COMPOSITE96':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/PCAWG/sa_PCAWG_composite96.tsv'), sep='\t').dropna(1)
        reference_index = 'Somatic Mutation Type'
    elif cosmic == 'pcawg_SBS_ID':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/PCAWG/sa_PCAWG_sbs_id.tsv'), sep='\t').dropna(1)
        reference_index = 'Somatic Mutation Type'
    elif cosmic == 'pcawg_SBS96_ID':
        reference = pd.read_csv(pkg_resources.resource_filename('signatureanalyzer', 'ref/PCAWG/sa_PCAWG_sbs96_id.tsv'), sep='\t').dropna(1)
        reference_index = 'Somatic Mutation Type'
    else:
        raise Exception("Not yet implemented for {}".format(cosmic))
    
    print("   * Using {} signatures".format(cosmic))
    return reference, reference_index

def compl(seq: str, reverse: bool = False):
    """
    Gets the complement of a string
    Args:
        * seq: string (does not have to be base pair)
        * reverse: set to true to reverse seq

    Returns:
        * complement of seq
    """
    return ''.join([COMPL[x] if x in COMPL.keys() else x for x in (reversed(seq) if reverse else seq)])

def sbs_annotation_converter(x: str) -> str:
    """
    Eithers swaps from word -> arrow format for SBS or vice versa.
        word: (REF)(ALT)(LEFT)(RIGHT)
        arrow: (LEFT)[(REF)>(ALT)](RIGHT)
    """
    if '>' in x:
        return x[2]+x[4]+x[0]+x[6]
    else:
        return x[2]+'['+x[0]+'>'+x[1]+']'+x[3]
    
def _map_id_sigs(
    df: pd.DataFrame,
    ) -> pd.Series:
    """
    Map Insertion-Deletion Substitution Signatures.
    -----------------------
    Args:
        * df: pandas.core.frame.DataFrame with index to be mapped

    Returns:
        * pandas.core.series.Series with matching indices to input cosmic
    """
    def _convert_to_cosmic(x):
        i1 = 'DEL' if 'del' in x else 'INS'
        if x[0].isdigit():
            i2 = 'MH' if 'm' in x else 'repeats'
            i3 = re.search('[\d+]+', x).group()
        else:
            i2 = x[0]
            i3 = '1'
        i4 = re.search('[\d+]+$', x).group()
        if i1 == 'DEL' and i2 != 'MH':
            i4 = str(int(i4[0]) - 1) + i4[1:]
        return '_'.join([i1, i2, i3, i4])

    if df.index.name is None: df.index.name = 'index'
    df_idx = df.index.name

    context_s = df.reset_index()[df_idx]
    return context_s.apply(_convert_to_cosmic)

def _map_dbs_sigs(
    df: pd.DataFrame,
    cosmic_df: pd.DataFrame,
    sub_index: str = 'Substitution Type'
    ) -> pd.Series:
    """
    Map Doublet-Base Substitution Signatures.
    -----------------------
    Args:
        * df: pandas.core.frame.DataFrame with index to be mapped
        * cosmic_df: dataframe with Cosmic indices to map to
        * sub_index: substitution index - the column to map to in the cosmic dataframe

    Returns:
        * pandas.core.series.Series with matching indices to input cosmic
    """
    def _check_to_flip(x, ref):
        if x in ref:
            return x
        else:
            return compl(x)

    if df.index.name is None: df.index.name = 'index'
    df_idx = df.index.name

    context_s = df.reset_index()[df_idx]
    return context_s.apply(lambda x: _check_to_flip(x, set(cosmic_df[sub_index])))

def _map_sbs_sigs(
    df: pd.DataFrame,
    cosmic_df: pd.DataFrame,
    cosmic_type: str,
    sub_index: str = 'Substitution Type',
    ) -> pd.Series:
    """
    Map Single-Base Substitution Signatures.
    -----------------------
    Args:
        * df: pandas.core.frame.DataFrame with index to be mapped
        * cosmic_df: dataframe with Cosmic indices to map to
        * sub_index: substitution index - the column to map to in the cosmic dataframe

    Returns:
        * pandas.core.series.Series with matching indices to input cosmic
    """
    if cosmic_type in ['pcawg_SBS', 'pcawg_COMPOSITE']:
        def _check_to_flip(x, ref):
            if x[3:-3] in ref:
                return x
            else:
                return compl(x)
    else:
        def _check_to_flip(x, ref):
            if x[2:-2] in ref:
                return x
            else:
                return compl(x)
            
    if df.index.name is None: df.index.name = 'index'
    df_idx = df.index.name

    if ">" not in df.index[0]:
        # Already in word format
        context_s = df.reset_index()[df_idx].apply(sbs_annotation_converter)
    else:
        # Already in arrow format
        context_s = df.reset_index()[df_idx]

    return context_s.apply(lambda x: _check_to_flip(x, set(cosmic_df[sub_index])))


def _map_composite_sigs(
    df: pd.DataFrame,
    cosmic_df: pd.DataFrame,
    cosmic_type: str,
    sub_index: str = 'Somatic Mutation Type'
    ) -> pd.Series:
    """
    Map composite signatures
    -----------------------
    Args:
        * df: pandas.core.frame.DataFrame with index to be mapped
    Returns:
        * pandas.core.series.Series with matching indices to input cosmic
    """
    if cosmic_type == 'pcawg_COMPOSITE':
        context_sbs_s = _map_sbs_sigs(df[df.index.isin(context1536)], cosmic_df.iloc[:1536], cosmic_type)
        context_dbs_s = _map_dbs_sigs(df[df.index.isin(context78)], cosmic_df.iloc[1536:1614])
    else:
        context_sbs_s = _map_sbs_sigs(df[df.index.isin(context96)], cosmic_df.iloc[:96], cosmic_type)
        context_dbs_s = _map_dbs_sigs(df[df.index.isin(context78)], cosmic_df.iloc[96:174])
    context_id_s = df[df.index.isin(context83)].index.to_series()
    return context_sbs_s.append(context_dbs_s).append(context_id_s)
    

def _map_sbs_id_sigs(
    df: pd.DataFrame,
    cosmic_df: pd.DataFrame,
    cosmic_type: str,
    sub_index: str = 'Somatic Mutation Type',
    ) -> pd.Series:
    """
    Map signatures for SBS + ID spectra.
    -----------------------
    Args:
        * df: pandas.core.frame.DataFrame with index to be mapped
    Returns:
        * pandas.core.series.Series with matching indices to input cosmic
    """
    if cosmic_type == 'pcwg_SBS_ID':
        context_sbs_s = _map_sbs_sigs(df[df.index.isin(context1536)], cosmic_df.iloc[:1536], cosmic_type)
    else:
        context_sbs_s = _map_sbs_sigs(df[df.index.isin(context96)], cosmic_df.iloc[:96], cosmic_type)
    context_id_s = df[df.index.isin(context83)].index.to_series()
    return context_sbs_s.append(context_id_s)

    
def postprocess_msigs(res: dict, cosmic: pd.DataFrame, cosmic_index: str, cosmic_type: str):
    """
    Post process ARD-NMF on mutational signatures.
    ------------------------
    Args:
        * res: results dictionary from ARD-NMF (see ardnmf function)
        * cosmic: cosmic pd.DataFrmae
        * cosmic_index: feature index column in cosmic
            ** ex. in cosmic_v2, "Somatic Mutation Type" columns map to
                A[C>A]A, A[C>A]C, etc.

    Returns:
        * None, edits res dictionary directly
    """
    if cosmic_type in ('cosmic2','cosmic3','cosmic3_exome'):
        res["Wraw"]["mut"] = _map_sbs_sigs(res["Wraw"], cosmic, cosmic_type).values
    elif cosmic_type == 'cosmic3_DBS':
        res["Wraw"]["mut"] = _map_dbs_sigs(res["Wraw"], cosmic).values
    elif cosmic_type == 'cosmic3_ID':
        res["Wraw"]["mut"] = _map_id_sigs(res["Wraw"]).values
    elif cosmic_type in ['pcawg_COMPOSITE', 'pcawg_COMPOSITE96']:
        # map with PCAWG
        res["Wraw"]["mut"] = _map_composite_sigs(res["Wraw"], cosmic, cosmic_type).values
        # load Sanger 96 SBS and map
        cosmic_df_96, cosmic_idx_96 = load_reference_signatures("cosmic3")
        if cosmic_type == 'pcawg_COMPOSITE':
            res["Wraw96"] = get96_from_1536(res["Wraw"][res["Wraw"].index.isin(context1536)])
            res["Wraw96"]["mut"] = _map_sbs_sigs(res["Wraw96"], cosmic_df_96, 'cosmic3').values
        else:
            res["Wraw96"] = res["Wraw"][res["Wraw"].index.isin(context96)]
            res["Wraw96"]["mut"] = _map_sbs_sigs(res["Wraw96"], cosmic_df_96, 'cosmic3').values
    elif cosmic_type == 'pcawg_SBS':
        # Map with PCAWG
        res["Wraw"]["mut"] = _map_sbs_sigs(res["Wraw"], cosmic, cosmic_type).values
        # load Sanger 96 SBS and map
        cosmic_df_96, cosmic_idx_96 = load_reference_signatures("cosmic3")
        res["Wraw96"] = get96_from_1536(res["Wraw"])
        res["Wraw96"]["mut"] = _map_sbs_sigs(res["Wraw96"], cosmic_df_96, 'cosmic3').values
    elif cosmic_type in ['pcawg_SBS_ID', 'pcawg_SBS96_ID']:
        # map with PCAWG
        res["Wraw"]["mut"] = _map_sbs_id_sigs(res["Wraw"], cosmic, cosmic_type).values
        # load Sanger 96 SBS and map
        cosmic_df_96, cosmic_idx_96 = load_reference_signatures("cosmic3")
        if cosmic_type == 'pcawg_SBS_ID':
            res["Wraw96"] = get96_from_1536(res["Wraw"][res["Wraw"].index.isin(context1536)])
            res["Wraw96"]["mut"] = _map_sbs_sigs(res["Wraw96"], cosmic_df_96, 'cosmic3').values
        else:
            res["Wraw96"] = res["Wraw"][res["Wraw"].index.isin(context96)]
            res["Wraw96"]["mut"] = _map_sbs_sigs(res["Wraw96"], cosmic_df_96, 'cosmic3').values
    else:
        raise Exception("Error: Invalid Cosmic Type (Not yet Implemented for {}".format(cosmic_type))
        
    # Column names of NMF signatures & COSMIC References
    nmf_cols = list(res["signatures"].columns[res["signatures"].columns.str.match('S\d+')])
    ref_cols = list(cosmic.columns[cosmic.dtypes == 'float64'])
    if cosmic_type in ('pcawg_SBS', 'pcawg_COMPOSITE','pcawg_COMPOSITE96','pcawg_SBS_ID','pcawg_SBS96_ID'):
        ref_cols_96 = list(cosmic_df_96.columns[cosmic_df_96.dtypes == 'float64'])
    
    # Create cosine similarity matrix
    #if cosmic_type not in ['pcawg_COMPOSITE','pcawg_COMPOSITE96','pcawg_SBS96_ID','pcawg_SBS_ID']:
    X = res["Wraw"].set_index("mut").join(cosmic.set_index(cosmic_index)).dropna(1).loc[:,nmf_cols+ref_cols]
    res["cosine"] = pd.DataFrame(cosine_similarity(X.T), index=X.columns, columns=X.columns).loc[ref_cols,nmf_cols]

    # For 1536, Composite, and Composite 96 context, transform to 96 SBS and create cosine similarity matrix with COSMIC signatures
    if "pcawg" in cosmic_type:
        X96 = res["Wraw96"].set_index("mut").join(cosmic_df_96.set_index(cosmic_idx_96)).dropna(1).loc[:,nmf_cols+ref_cols_96]
        res["cosine_cosmic"] = pd.DataFrame(cosine_similarity(X96.T), index=X96.columns, columns=X96.columns).loc[ref_cols_96,nmf_cols]
        
        # Construct 96 context W matrix
        if cosmic_type in ["pcawg_SBS","pcawg_COMPOSITE", "pcawg_SBS_ID"]:
            W96 = get96_from_1536(res["W"][res["W"].index.isin(context1536)].copy().drop(columns=['max','max_id','max_norm']))
        else:
            W96 = res["W"][res["W"].index.isin(context96)].copy().drop(columns=['max','max_id','max_norm'])
        W96.columns = range(1,W96.shape[1]+1)
        Wnorm = W96.copy()
        for j in range(W96.shape[1]):
            Wnorm.iloc[:,j] *= res['H'].sum(1).values[j]
        Wnorm = Wnorm.div(Wnorm.sum(1),axis=0)
        W96_max_id = W96.idxmax(axis=1,skipna=True).astype('int')
        W96['max'] = W96.max(axis=1, skipna=True)
        W96['max_id'] = W96_max_id
        W96['max_norm']= Wnorm.max(axis=1, skipna=True)
        _rename = {x+1:'S'+ str(x+1) for x in range(len(list(res['H'])[:-3]))}
        res["W96"] = W96.rename(columns=_rename)
        
        # Add assignments
        s_assign96 = dict(res["cosine_cosmic"].idxmax())
        s_assign96 = {key:key+"-" + s_assign96[key] for key in s_assign96}
        res["cosine_cosmic"] = res["cosine_cosmic"].rename(columns=s_assign96)
        res["Wraw96"] = res["Wraw96"].rename(columns=s_assign96)
        res["W96"] = res["W96"].rename(columns=s_assign96)
        
            
    # Add assignments
    s_assign = dict(res["cosine"].idxmax())
    s_assign = {key:key+"-" + s_assign[key] for key in s_assign}

    # Update column names
    res["W"] = res["W"].rename(columns=s_assign)
    res["H"] = res["H"].rename(columns=s_assign)
    res["Wraw"] = res["Wraw"].rename(columns=s_assign)
    res["Hraw"] = res["Hraw"].T.rename(columns=s_assign)
    res["cosine"] = res["cosine"].rename(columns=s_assign)

def assign_signature_weights_to_maf(maf: pd.DataFrame, W: pd.DataFrame, H: pd.DataFrame):
    """
    Assign probabilities for each signature to each mutation in the maf
    ------------------------
    Args:
        * maf: input maf as a pd.DataFrame
        * W: W-matrix from NMF
        * H: H-matrix from NMF

    Returns:
        Maf with columns appended to the end for the weights of each signature
    """
    sig_columns = W.columns[W.columns.str.startswith('S')]
    W_prob = W.loc[maf[W.index.name], sig_columns].reset_index(drop=True)
    H_prob = H.loc[maf['sample'], sig_columns].reset_index(drop=True)
    signature_probabilities = W_prob * H_prob
    signature_probabilities /= np.sum(signature_probabilities.values, axis=1, keepdims=True)
    maf[sig_columns] = signature_probabilities
    return maf

# ---------------------------------
# Parsing Output H5 Utils
# ---------------------------------
def get_nruns_from_output(file: str) -> int:
    """
    Get number of NMF runs from an .h5 output file.
    ------------------------
    Args:
        * output file from NMF (.h5)

    Returns:
        * number of NMF runs (int)
    """
    with h5py.File(file) as f:
        return(max([int(x.split('run')[1]) for x in list(f.keys()) if 'run' in x])+1)

def get_nlogs_from_output(file: str) -> pd.DataFrame:
    """
    Returns a dataframe of the final output log from each run.
    ------------------------
    Args:
        * output file from NMF (.h5)

    Returns:
        * pd.DataFrame of reporting statistics for each run
    """
    n_runs = get_nruns_from_output(file)
    df = pd.concat([pd.read_hdf(file,"run{}/log".format(i)).reset_index().iloc[-1] for i in range(n_runs)],1).T
    df.index = np.arange(n_runs)
    return df

# ---------------------------------
# Preprocessing input mafs
# ---------------------------------
def get_dnps_from_maf(maf: pd.DataFrame):
    """
    Get DNPs from a maf which has adjacent SNPs
    ________________________
    Args:
        * maf: maf DataFrame

    Returns:
        * pd.DataFrame of maf with only adjacent SNPs (annotated as DNPs)
    """
    sub_mafs = []
    for _, df in maf.loc[maf['Variant_Type'] == 'SNP'].groupby(['sample', 'Chromosome']):
        df = df.sort_values('Start_position')
        start_pos = np.array(df['Start_position'])
        pos_diff = np.diff(start_pos)
        idx = []
        if len(pos_diff) == 1 and pos_diff[0] == 1:
            idx.append(0)
        if len(pos_diff) >= 2 and pos_diff[0] == 1 and pos_diff[1] > 1:
            idx.append(0)
        idx.extend(np.flatnonzero((pos_diff[:-2] > 1) & (pos_diff[1:-1] == 1) & (pos_diff[2:] > 1)) + 1)
        if len(pos_diff) >= 2 and pos_diff[-1] == 1 and pos_diff[-2] > 1:
            idx.append(len(pos_diff) - 1)
        if idx:
            idx = np.array(idx)
            rows = df.iloc[idx][['Hugo_Symbol', 'Tumor_Sample_Barcode', 'sample',
                'Chromosome', 'Start_position', 'Reference_Allele', 'Tumor_Seq_Allele2']].reset_index(drop=True)
            rows_plus_one = df.iloc[idx + 1].reset_index()
            rows['Variant_Type'] = 'DNP'
            rows['End_position'] = rows['Start_position'] + 1
            rows['Reference_Allele'] = rows['Reference_Allele'] + rows_plus_one['Reference_Allele']
            rows['Tumor_Seq_Allele2'] = rows['Tumor_Seq_Allele2'] + rows_plus_one['Tumor_Seq_Allele2']
            sub_mafs.append(rows)
    return pd.concat(sub_mafs).reset_index(drop=True)

def get_true_snps_from_maf(maf: pd.DataFrame):
    """
    Get SNPs from a maf which has adjacent SNPs
    ________________________
    Args:
        * maf: maf DataFrame

    Returns:
        * pd.DataFrame of maf with adjacent SNPs filtered out
    """
    sub_mafs = []

    # Check if MAF includes End_position
    if not 'End_position' in list(maf):
        def get_endpos(row):
            if row['Variant_Type'] in ['SNP', 'DEL']:
                return int(row['Start_position']) + len(row['Reference_Allele']) - 1
            else:
                return int(row['Start_position']) + 1
        maf['End_position'] = maf.apply(get_endpos, axis=1)
    
    for _, df in maf.loc[maf['Variant_Type'].isin(['SNP','DEL',])].groupby(['sample', 'Chromosome']):
        df = df.sort_values('Start_position')
        start_pos = np.array(df['Start_position'])
        end_pos = np.array(df['End_position'])
        pos_diff = np.diff(start_pos)
        pos_diff_end = np.diff(end_pos)
        rem_idx = np.flatnonzero(pos_diff <= 1)
        rem_idx = np.concatenate([rem_idx, rem_idx + 1])
        rem_idx_end = np.flatnonzero(pos_diff_end <= 1)
        rem_idx_end = np.concatenate([rem_idx_end, rem_idx_end + 1])
        idx = np.delete(np.arange(len(start_pos)), np.append(rem_idx,rem_idx_end))
        if len(idx):
            rows = df.iloc[idx]
            sub_mafs.append(rows)
    temp = pd.concat(sub_mafs).reset_index(drop=True)
    return temp[temp['Variant_Type']=='SNP']


def get96_from_1536(W1536):
    """
    Convert 1536 W matrix to 96 context W matrix to extract cosmic signatures
    ________________________
    Args:
        * W1536: 1536 context W matrix
    Returns:
        * pd.Dataframe of 96 context matrix
    """
    context96_df = pd.DataFrame(0, index=context96 , columns=W1536.columns)
    
    # Define conversion function based on format
    if ">" in W1536.index[0]:
        def convert(x):
            sbs = x[3] + x[5] + x[1] + x[7]
            if sbs[0] not in ['C','A']:
                sbs = compl(sbs[:2]) + compl(sbs[-1]) + compl(sbs[-2])
            return sbs
    else:
        def convert(x):
            sbs = x[:2] + x[3:5]
            if sbs[0] not in ['C','A']:
                sbs = compl(sbs[:2]) + compl(sbs[-1]) + compl(sbs[-2])
            return sbs
        
    # For each context in 96 SNV, sum all corresponding 1536 context rows
    for context in context96:
        context96_df.loc[context] = W1536[W1536.index.map(convert) == context].sum()
    return context96_df

def get_pole_pold_muts(maf: pd.DataFrame):
    """
    Prints sets of samples with POLE-exo mutation, POLD-exo mutation, and
    POLE-exo + POLD-exo mutation
    """
    pole_res = (223,517)
    pold_res = (245-571)
    if 'UniProt_AApos' in list(maf) or 'HGVSp_Short' in list(maf):
        if 'HGVSp_Short' in list(maf) and 'UniProt_AApos' not in list(maf):
            table = maf[maf['Hugo_Symbol'].isin(['POLE','POLD1'])][['Hugo_Symbol','Variant_Classification','Variant_Type','HGVSp_Short', 'Tumor_Sample_Barcode']].copy()
            table = table[table['Variant_Classification']=='Missense_Mutation']
            table.rename(columns={'HGVSp_Short':'UniProt_AApos'})
            get_pos = lambda x: int(''.join(c for c in x if c.isdigit()))
            table['UniProt_AApos'] = table['HGVSp_Short'].map(get_pos)
        else:
            table = maf[maf['Hugo_Symbol'].isin(['POLE','POLD1'])][['Hugo_Symbol','Variant_Classification','Variant_Type','UniProt_AApos', 'Tumor_Sample_Barcode']].copy()
            table = table[table['Variant_Classification'] == 'Missense_Mutation']
        pole = []
        pold = []
        for m in table.index:
            if table.loc[m,'Hugo_Symbol'] == 'POLE':
                if table.loc[m,'UniProt_AApos'] >= pole_res[0] and table.loc[m,'UniProt_AApos'] <= pole_res[1]:
                    pole.append(table.loc[m,'Tumor_Sample_Barcode'])
            else:
                if table.loc[m,'UniProt_AApos'] >= pole_res[0] and table.loc[m,'UniProt_AApos'] <= pole_res[1]:
                    pold.append(table.loc[m,'Tumor_Sample_Barcode'])
        stdout.write("POLE-exo* patients:\n{}\n".format(np.unique(pole)))
        stdout.write("POLD-exo* patients:\n{}\n".format(np.unique(pold)))
        stdout.write("POLE-exo* + POLD-exo* patients:\n{}\n".format(np.intersect1d(pole,pold)))
    else:
        stdout.write("Neither UniProt_AApos nor HGVSp_Short were found in the maf columns. Please try again with one of these columns")
    return np.unique(pole), np.unique(pold)

def plot_mutational_signatures(outdir, reference):
    # Import plotting functions
    from .plotting import k_dist, signature_barplot, stacked_bar, signature_barplot_DBS, signature_barplot_ID, signature_barplot_composite, cosine_similarity_plot
    
    print("   * Saving report plots to {}".format(outdir))
    H = pd.read_hdf(os.path.join(outdir,'nmf_output.h5'), "H")
    W = pd.read_hdf(os.path.join(outdir,'nmf_output.h5'), "W")
    cosine = pd.read_hdf(os.path.join(outdir,'nmf_output.h5'), "cosine")
    
    if reference == 'cosmic3_DBS':
        _ = signature_barplot_DBS(W, contributions=np.sum(H))
    elif reference == 'cosmic3_ID':
        _ = signature_barplot_ID(W, contributions=np.sum(H))
    elif reference == 'pcawg_SBS':
        #
        # COSMIC cosine similarity, COSMIC attribution stacked barplot
        # PCAWG contribution barplot
        #
        H96 = H.copy()
        W96 = pd.read_hdf(os.path.join(outdir, 'nmf_output.h5'), "W96")
        H96.columns = W96.columns
        # Plot 96 COSMIC cosine similarity
        _ = cosine_similarity_plot(pd.read_hdf(os.path.join(outdir,'nmf_output.h5'), "cosine_cosmic"))
        plt.savefig(os.path.join(outdir, "cosine_similarity_plot_96.pdf"), dpi=320, bbox_inches='tight')
        # Plot COSMIC Signature Attribution Stacked Barplot
        _ = stacked_bar(H96, 'cosmic3')
        plt.savefig(os.path.join(outdir, "signature_stacked_barplot_cosmic.pdf"), dpi=320, bbox_inches='tight')
        # Plot signature contribution barplot collapsed to 96 SBS
        _ = signature_barplot(W96, contributions=np.sum(H96))
    elif reference in ['pcawg_COMPOSITE','pcawg_COMPOSITE96']:
        #
        # COSMIC cosine similarity, COSMIC attribution stacked barplot, COSMIC contribution barplot
        # PCAWG contribution barplot
        #
        H96 = H.copy()
        W96 = pd.read_hdf(os.path.join(outdir, 'nmf_output.h5'), "W96")
        H96.columns = W96.columns
        # COSMIC cosine similarity
        _ = cosine_similarity_plot(pd.read_hdf(os.path.join(outdir,'nmf_output.h5'), "cosine_cosmic"))
        plt.savefig(os.path.join(outdir, "cosine_similarity_plot_96.pdf"), dpi=320, bbox_inches='tight')
        # COSMIC attribution stacked barplot
        _ = stacked_bar(H96, 'cosmic3')
        plt.savefig(os.path.join(outdir,'signature_stacked_barplot_cosmic.pdf'), dpi=320, bbox_inches='tight')
        # COSMIC contribution barplot
        _ = signature_barplot(W96, contributions=np.sum(H96))
        plt.savefig(os.path.join(outdir, "signature_contributions_COSMIC.pdf"), dpi=320,bbox_inches='tight')
        # PCAWG contribution barplot
        if reference == 'pcawg_COMPOSITE':
            W_plot = pd.concat([get96_from_1536(W[W.index.isin(context1536)]),W[~W.index.isin(context1536)]])
        else:
            W_plot = W
        _ = signature_barplot_composite(W_plot, contributions=np.sum(H))
    elif reference in ['pcawg_SBS_ID', 'pcawg_SBS96_ID']:
        #
        # COSMIC cosine similarity, COSMIC attribution stacked barplot, COSMIC contribution barplot
        # PCAWG contribution barplot
        #
        H96 = H.copy()
        W96 = pd.read_hdf(os.path.join(outdir, 'nmf_output.h5'), "W96")
        H96.columns = W96.columns
        # COSMIC cosine similarity
        _ = cosine_similarity_plot(pd.read_hdf(os.path.join(outdir,'nmf_output.h5'), "cosine_cosmic"))
        plt.savefig(os.path.join(outdir, "cosine_similarity_plot_96.pdf"), dpi=320, bbox_inches='tight')
        # COSMIC attribution stacked barplot
        _ = stacked_bar(H96, 'cosmic3')
        plt.savefig(os.path.join(outdir,'signature_stacked_barplot_cosmic.pdf'), dpi=320, bbox_inches='tight')
        # COSMIC contribution barplot
        _ = signature_barplot(W96, contributions=np.sum(H96))
        plt.savefig(os.path.join(outdir, "signature_contributions_COSMIC.pdf"), dpi=320,bbox_inches='tight')
        # PCAWG contribution barplot
        if reference == 'pcawg_SBS_ID':
            W_plot = pd.concat([get96_from_1536(W[W.index.isin(context1536)]),W[~W.index.isin(context1536)]])
        else:
            W_plot = W
        _ = signature_barplot_sbs_id(W_plot, contributions=np.sum(H))
    else:
        _ = signature_barplot(W, contributions=np.sum(H))
        
        # Plot signature contributions, attribution stacked barplot, K distribution, and cosine similarity
        plt.savefig(os.path.join(outdir, "signature_contributions.pdf"), dpi=320, bbox_inches='tight')
        _ = stacked_bar(H,reference)
        plt.savefig(os.path.join(outdir, "signature_stacked_barplot.pdf"), dpi=320, bbox_inches='tight')
        _ = k_dist(np.array(aggr.K, dtype=int))
        plt.savefig(os.path.join(outdir, "k_dist.pdf"), dpi=320, bbox_inches='tight')
        _ = cosine_similarity_plot(cosine)
        plt.savefig(os.path.join(outdir, "cosine_similarity_plot.pdf"), dpi=320, bbox_inches='tight')
