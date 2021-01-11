import os, sys, re, json, random, copy, argparse, pickle, importlib
import numpy as np
import pandas as pd
from collections import OrderedDict 
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
import logomaker as lm
from util import *
import warnings
warnings.filterwarnings('ignore')


class Predictor():
    def __init__(self, mhc_encode_dict, model_file, model_state_files, encoding_method):
        # MHC binding domain encoding
        self.mhc_encode_dict = mhc_encode_dict

        # device: gpu or cpu
        if torch.cuda.is_available():
            self.device = torch.device('cuda')
            self.batch_size = 4096
        else:
            self.device = torch.device('cpu')
            self.batch_size = 64

        # model
        if encoding_method == 'onehot':
            dim = 21
        elif encoding_method == 'blosum':
            dim = 24
        else:
            print("Wrong encoding method")
            raise ValueError
        model_file = '.'.join(model_file.split('.')[0].split('/'))
        module = importlib.import_module(model_file)
        self.model = module.CombineModel(module.MHCModel(dim), module.EpitopeModel(dim))

        # model states
        self.models = OrderedDict()
        for i in range(len(model_state_files)):
            basename = re.split(r'[\/\.]', model_state_files[i])[-2]
            model_state_dict = torch.load(model_state_files[i], map_location=self.device)
            self.models[basename] = copy.deepcopy(self.model)
            self.models[basename].load_state_dict(model_state_dict['model_state_dict'])
            self.models[basename].to(self.device)


    def __call__(self, df, dataset, allele=None):
        result_df = pd.DataFrame(index=df.index, columns=list(self.models.keys()))
        result_df['sequence'] = df['sequence']

        # general mode
        if allele:
            dataloader = torch.utils.data.DataLoader(dataset, batch_size=self.batch_size, shuffle=False)
            preds = self._predict(allele, dataloader)
            result_df.loc[:, list(self.models.keys())] = preds

        # specific mode
        else:
            result_df['mhc'] = df['mhc']
            for allele, sub_df in tqdm(df.groupby('mhc'), desc='alleles', leave=False, position=0):
                idx = sub_df.index
                sub_dataset = torch.utils.data.Subset(dataset, idx)
                sub_dataloader = torch.utils.data.DataLoader(sub_dataset, batch_size=self.batch_size, shuffle=False)
                preds = self._predict(allele, sub_dataloader)
                result_df.loc[idx, list(self.models.keys())] = preds

        return result_df


    def _predict(self, allele, dataloader):
        mhc_encode = self.mhc_encode_dict[allele]
        df = pd.DataFrame()
        for key, model in tqdm(self.models.items(), desc='models', leave=False, position=1):
            for j, (x,y) in enumerate(tqdm(dataloader, desc='batches', leave=False, position=2)):
                with torch.no_grad():
                    model.eval()
                    num = x.shape[0]
                    epitope_encode = x.to(self.device).float()
                    mhc_encode_tile = torch.FloatTensor(np.tile(mhc_encode, (num, 1, 1))).to(self.device)
                    pred = model(mhc_encode_tile, epitope_encode).to('cpu')
                    pred = pred.view(-1,).numpy()
                    if j==0:
                        preds = pred
                    else:
                        preds = np.append(preds, pred, axis=0)
            df[key] = preds
        return df.values


class Interpretation():
    def __init__(self, interpretation_file, mhc_dict, output_dir):
        self.aa_str = 'ACDEFGHIKLMNPQRSTVWY'
        self.sub_motif_len = 4
        self.dpi = 600
        self.fontsize = 10
        
        self.interp_dict = pickle.load(open(interpretation_file, 'rb'))
        self.positions = self.interp_dict['important_positions']
        self.mhc_dict = mhc_dict
        self.output_dir = output_dir
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)


    def __call__(self, allele, seqs):
        hla = allele.split('*')[0]
        motif_df = self._get_motif_seqlogo(seqs, self.sub_motif_len)
        allele_df = self._get_allele_seqlogo(allele)

        fig, ax = plt.subplots(4, 2, figsize=(8, 10), dpi=self.dpi,
                                 gridspec_kw={'width_ratios': [1, 3]})
        current_ax = 0

        for side in ['N', 'C']:
            cluster = self.interp_dict['%s%s_label'%(hla, side)][allele]
            
            if not cluster in self.interp_dict['%s%s_allele_signature'%(hla, side)].keys():
                print("The cluster of %s is too small to interpretate"%allele)
                return

            hyper_motif = self.interp_dict['%s%s_hyper_motif'%(hla, side)][cluster]
            hyper_motif = pd.DataFrame(hyper_motif, columns=list(self.aa_str))
            allele_signature = self.interp_dict['%s%s_allele_signature'%(hla, side)][cluster]
            allele_signature = pd.DataFrame(allele_signature, columns=list(self.aa_str))

            # plot cluster
            self._motif_plot(hyper_motif, side, ax[current_ax][0], title='%s-side hyper-motif'%side)
            self._mhcseq_plot(allele_signature, ax[current_ax][1], title='%s-side allele signature'%side)
            current_ax += 1

            # plot allele itself
            if side == 'N':
                temp_df = motif_df.iloc[:4]
            else:
                temp_df = motif_df.iloc[-4:].reset_index(drop=True)
            self._motif_plot(temp_df, side, ax[current_ax][0], title='%s, %s-side motif, num=%d'%(allele, side, len(seqs)))
            allele_df[allele_df > 0] = 1
            allele_signature[allele_signature < 0] = 0
            self._mhcseq_plot(allele_df * allele_signature, ax[current_ax][1], title='%s, %s-side highlighted allele signature'%(allele, side))
            current_ax += 1

        fig.tight_layout()
        fig.savefig('%s/%s%s%s.png'%(self.output_dir, hla, allele[2:4], allele[5:]))

        return motif_df


    def _get_motif_seqlogo(self, seqs, sub_motif_len):
        seqs = seqs.apply(lambda x: x[:sub_motif_len] + x[-sub_motif_len:])
        seqlogo_df = lm.alignment_to_matrix(sequences=seqs, to_type='information', characters_to_ignore='XU.')
        df = pd.DataFrame(columns=list(self.aa_str))
        df = pd.concat([df, seqlogo_df], axis=0)
        df = df[list(self.aa_str)]
        df = df.fillna(0.0)
        return df


    def _get_allele_seqlogo(self, allele):
        seq = self.mhc_dict[allele]
        seq = ''.join([seq[i] for i in self.positions])
        seqlogo_df = lm.alignment_to_matrix(sequences=[seq], to_type='probability', characters_to_ignore=".XU", pseudocount=0)
        df = pd.DataFrame(columns=list(self.aa_str))
        df = pd.concat([df, seqlogo_df], axis=0)
        df = df[list(self.aa_str)]
        df = df.fillna(0.0)
        return df


    def _motif_plot(self, seqlogo_df, side, ax, ylim=4, title=None, turn_off_label=False):
        if side == 'N':
            xticklabels = list(range(1, self.sub_motif_len+1))
        else:
            xticklabels = list(range(-self.sub_motif_len, 0))
        logo = lm.Logo(seqlogo_df, color_scheme='skylign_protein', ax=ax)
        _ = ax.set_xticks(list(range(len(xticklabels))))
        _ = ax.set_xticklabels(xticklabels)
        _ = ax.set_ylim(0,ylim)
        _ = ax.set_title(title)
        for item in ([ax.title, ax.xaxis.label, ax.yaxis.label] + ax.get_xticklabels() + ax.get_yticklabels()):
            item.set_fontsize(self.fontsize)
            
        if turn_off_label:
            _ = ax.set_xticks([])
            _ = ax.set_yticks([])
            _ = ax.set_xticklabels([])
            _ = ax.set_yticklabels([])
            _ = ax.set_title(None)


    def _mhcseq_plot(self, seqlogo_df, ax, ylim=1, title=None, turn_off_label=False):
        logo = lm.Logo(seqlogo_df, color_scheme='skylign_protein', ax=ax)
        _ = ax.set_ylim(-ylim, ylim)
        _ = ax.set_xticks(range(len(self.positions)))
        _ = ax.set_xticklabels([i+1 for i in self.positions], rotation=90)
        _ = ax.set_title(title)
        for item in ([ax.title, ax.xaxis.label, ax.yaxis.label] + ax.get_xticklabels() + ax.get_yticklabels()):
            item.set_fontsize(self.fontsize)
        for item in ax.get_xticklabels():
            item.set_fontsize(self.fontsize-3)
            
        if turn_off_label:
            _ = ax.set_xticks([])
            _ = ax.set_yticks([])
            _ = ax.set_xticklabels([])
            _ = ax.set_yticklabels([])
            _ = ax.set_title(None)


def ArgumentParser():
    description = '''
    MHCfovea, an MHCI-peptide binding predictor. In this prediction process, GPU is recommended.
    
    Having two modes:
    1. specific mode: each peptide has its corresponding MHC-I allele in the input file; column "mhc" or "allele" is required
    2. general mode: all peptides are predicted with all alleles in the "alleles" argument
    
    Input file:
    only .csv file is acceptable
    column "sequence" or "peptide" is required as peptide sequences
    column "mhc" or "allele" is optional as MHC-I alleles
    
    Output directory contains:
    1. prediction.csv: with new column "score" for specific mode or [allele] for general mode
    2. interpretation: a directory contains interpretation figures of each allele with more than 10 positive predictions
    3. metrics.json: all and allele-specific metrics (AUC, AUC0.1, AP, PPV); column "bind" as benchmark is required
    '''
    
    parser = argparse.ArgumentParser(prog='predictor', description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('input', help='The input file')
    parser.add_argument('output_dir', help='The output directory')
    parser.add_argument('--alleles', required=False, default=None, help='alleles for general mode')
    parser.add_argument('--motif_threshold', required=False, default=0.9, help='prediction threshold for epitope-binding motifs, default=0.9')
    parser.add_argument('--get_metrics', required=False, default=False, action='store_true', help='calculate the metrics between prediction and benchmark')

    return parser


def main(args=None):
    """""""""""""""""""""""""""""""""""""""""
    # Arguments
    """""""""""""""""""""""""""""""""""""""""
    args = ArgumentParser().parse_args(args)
    current_dir = os.path.abspath(os.getcwd())
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # input
    if args.input.startswith('/'):
        peptide_dataframe = args.input
    else:
        peptide_dataframe = '%s/%s'%(current_dir, args.input)

    # output
    if args.output_dir.startswith('/'):
        output_dir = args.output_dir
    else:
        output_dir = '%s/%s'%(current_dir, args.output_dir)
    if not os.path.isdir(output_dir):
        os.mkdir(output_dir)

    # data
    mhc_file = '../data/MHCI_res182_seq.json'
    mhc_dict = json.load(open(mhc_file, 'r'))
    alleles = args.alleles
    encoding_method = 'onehot'
    interpretation_file = '../data/interpretation.pkl'

    # model
    model_file = 'model.py'
    model_state_dir = 'model_state'

    # others
    seqlogo_threshold = args.motif_threshold
    positive_threshold = 10
    get_metrics = args.get_metrics


    """""""""""""""""""""""""""""""""""""""""
    # Loading Data & Model
    """""""""""""""""""""""""""""""""""""""""
    print("Loading data and model...")

    # model state files
    model_state_files = list()
    for file in os.listdir(model_state_dir):
        model_state_files.append('%s/%s'%(model_state_dir, file))
    model_state_files.sort()

    # peptide dataframe
    df = pd.read_csv(peptide_dataframe)
    df = df.rename(columns={'peptide':'sequence', 'peptides':'sequence', 'sequences':'sequence',
                            'allele':'mhc', 'alleles':'mhc', 'binds':'bind'})
    ## check column "sequence"
    if 'sequence' not in df.columns:
        print('input file has no peptide sequence column; column name has to be "sequence" or "peptide"')
        raise ValueError
    cols = ['sequence']
    ## check column "mhc"
    if not args.alleles:
        if 'mhc' not in df.columns:
            print('input file has no MHC-I allele column; column name has to be "mhc" or "allele"')
            raise ValueError
        df['mhc'] = df['mhc'].apply(lambda x: ConvertAllele(x))
        cols.append('mhc')
        alleles = list(df['mhc'].unique())
    else:
        alleles = [ConvertAllele(x) for x in args.alleles.split(',')]
    ## check column "bind"
    if get_metrics:
        if 'bind' not in df.columns:
            print('input file has no benchmark column; column name has to be "bind"')
            raise ValueError
        cols.append('bind')
    df = df[cols]
    
    # peptide dataset
    dataset = BuildDataset(df, 'onehot', 15, with_label=get_metrics)

    # mhc encoding dict
    mhc_encode_dict = dict()
    for allele in alleles:
        mhc_encode_dict[allele] = OneHotEncoder(mhc_dict[allele], 182, True)

    
    """""""""""""""""""""""""""""""""""""""""
    # Prediction & Interpretation
    """""""""""""""""""""""""""""""""""""""""
    print("Predicting...")

    # predictor
    Pred = Predictor(mhc_encode_dict, model_file, model_state_files, encoding_method)

    # interpretation
    Interp = Interpretation(interpretation_file, mhc_dict, '%s/interpretation'%output_dir)

    # seqlogo dict
    seqlogo_dict = dict()

    # general mode
    if args.alleles:
        metrics_dict = dict()
        for allele in tqdm(alleles, desc='alleles', leave=False, position=0):
            pred_df = Pred(df, dataset, allele=allele)
            df[allele] = pred_df[list(Pred.models.keys())].mean(axis=1).round(3)

            # seqlogo
            idx = np.where(df[allele] > seqlogo_threshold)[0]
            if len(idx) >= positive_threshold:
                seqlogo_dict[allele] = Interp(allele, df.iloc[idx]['sequence']).values

            # metrics
            if get_metrics:
                metrics_dict[allele] = CalculateMetrics(df['bind'], df[allele])

    # specific mode
    else:
        pred_df = Pred(df, dataset)
        df['score'] = pred_df[list(Pred.models.keys())].mean(axis=1).round(3)
        
        # seqlogo
        for allele, sub_df in df.groupby('mhc'):
            idx = np.where(sub_df['score'] > seqlogo_threshold)[0]
            if len(idx) >= positive_threshold:
                seqlogo_dict[allele] = Interp(allele, sub_df.iloc[idx]['sequence']).values

        # metrics
        if get_metrics:
            all_metrics = CalculateMetrics(df['bind'], df['score'])
            allele_metrics = CalculateAlleleMetrics(df['mhc'], df['bind'], df['score'])
            metrics_dict = {'all': all_metrics, **allele_metrics}


    """""""""""""""""""""""""""""""""""""""""
    # Save result and record
    """""""""""""""""""""""""""""""""""""""""
    # result
    df.to_csv('%s/prediction.csv'%output_dir, index=False)
    ##np.save('%s/motif.npy'%output_dir, seqlogo_dict)
    if get_metrics:
        json.dump(metrics_dict, open('%s/metrics.json'%output_dir, 'w'))

    print('Done')


if __name__=="__main__":
    main()
    
