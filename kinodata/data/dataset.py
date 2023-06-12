import multiprocessing as mp
import os
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Dict, List

import pandas as pd
import requests  # type : ignore
import torch
from rdkit import RDLogger
from rdkit.Chem import PandasTools
from torch_geometric.data import HeteroData, InMemoryDataset
from torch_geometric.transforms import Compose
from tqdm import tqdm

from kinodata.data.featurization.pocket import add_pocket_information
from kinodata.data.featurization.ligand import add_atoms, add_bonds
from kinodata.data.featurization.kissim import (
    add_kissim_fp,
    load_kissim,
    PHYSICOCHEMICAL,
    STRUCTURAL,
)
from kinodata.transform.add_distances import AddDistances, AddDistancesAndInteractions
from kinodata.transform.filter_activity import (
    FilterActivityScore,
    FilterActivityType,
    FilterCombine,
)

_DATA = Path(__file__).parents[2] / "data"


class KinodataDocked(InMemoryDataset):
    def __init__(
        self,
        root: str = str(_DATA),
        remove_hydrogen: bool = False,
        transform: Callable = None,
        pre_transform: Callable = None,
        pre_filter: Callable = (lambda _: True),
        post_filter: Callable = FilterCombine(
            [FilterActivityType(["pIC50"]), FilterActivityScore()]
        ),
    ):
        self.remove_hydrogen = remove_hydrogen
        self.post_filter = post_filter
        super().__init__(root, transform, pre_transform, pre_filter)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def raw_file_names(self) -> List[str]:
        return ["kinodata_docked_filtered.sdf.gz"]

    @property
    def processed_file_names(self) -> List[str]:
        # TODO add preprocessed kssim fingerprints?
        return ["kinodata_docked.pt"]

    @property
    def pocket_dir(self) -> Path:
        return Path(self.raw_dir) / "mol2" / "pocket"

    def download(self):
        # TODO at some point set up public download?
        pass

    @cached_property
    def df(self) -> pd.DataFrame:
        print("Reading data frame..")
        df = PandasTools.LoadSDF(
            self.raw_paths[0],
            smilesName="compound_structures.canonical_smiles",
            molColName="molecule",
            embedProps=True,
            removeHs=self.remove_hydrogen,
        )
        df.set_index("ID", inplace=True)

        print("Checking for missing pocket mol2 files...")
        df["similar.klifs_structure_id"] = df["similar.klifs_structure_id"].astype(int)
        # get pocket mol2 files
        if not self.pocket_dir.exists():
            self.pocket_dir.mkdir(parents=True)

        struc_ids = df["similar.klifs_structure_id"].unique()
        pbar = tqdm(struc_ids, total=len(struc_ids))
        for structure_id in pbar:
            fp = self.pocket_dir / f"{structure_id}_pocket.mol2"
            if fp.exists():
                continue
            resp = requests.get(
                "https://klifs.net/api/structure_get_pocket",
                params={"structure_ID": structure_id},
            )
            resp.raise_for_status()
            fp.write_bytes(resp.content)

        pocket_mol2_files = {
            int(fp.stem.split("_")[0]): fp for fp in (self.pocket_dir).iterdir()
        }
        df["pocket_mol2_file"] = [
            pocket_mol2_files[row["similar.klifs_structure_id"]]
            for _, row in df.iterrows()
        ]

        # backwards compatability
        df["ident"] = df.index

        return df

    def process(self):

        # TODO
        # <add kissim preprocessing> (?)

        RDLogger.DisableLog("rdApp.*")

        data_list = []
        skipped: List[str] = []

        tasks = [
            (
                ident,
                row["compound_structures.canonical_smiles"],
                row["molecule"],
                float(row["activities.standard_value"]),
                row["activities.standard_type"],
                row["pocket_mol2_file"],
                float(row["docking.chemgauss_score"]),
                float(row["docking.posit_probability"]),
                int(row["similar.klifs_structure_id"]),
            )
            for ident, row in tqdm(
                self.df.iterrows(),
                desc="Creating PyG object tasks..",
                total=len(self.df),
            )
        ]

        with mp.Pool(os.cpu_count()) as pool:
            data_list = pool.map(process_idx, tasks)

        # data_list = list(map(process_idx, tasks))

        skipped = [
            ident for ident, data in zip(self.df.index, data_list) if data is None
        ]
        data_list = [d for d in data_list if d is not None]
        if len(skipped) > 0:
            print(f"Skipped {len(skipped)} unprocessable entries.")
            (Path(self.root) / "skipped_idents.log").write_text("\n".join(skipped))

        if self.pre_filter is not None:
            print("Applying pre filter..")
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            print("Applying pre transform..")
            data_list = [self.pre_transform(data) for data in data_list]

        if self.post_filter is not None:
            print("Applying post filter..")
            data_list = [data for data in data_list if self.post_filter(data)]

        data, slices = self.collate(data_list)
        torch.save((data, slices), self.processed_paths[0])

    def ident_index_map(self) -> Dict[Any, int]:
        # this may be very slow if self.transform is computationally expensive
        mapping = [(int(data.ident), index) for index, data in enumerate(self)]
        return dict(mapping)


def process_chunk(tasks) -> List:
    return list(map(process_idx, tasks))


def process_idx(args):
    (
        ident,
        smiles,
        ligand,
        activity,
        activity_type,
        pocket_file,
        docking_score,
        posit_prob,
        structure_id,
    ) = args
    data = HeteroData()

    data = add_atoms(ligand, data, "ligand")
    data = add_bonds(ligand, data, "ligand")
    data = add_pocket_information(data, pocket_file)
    if data is None:
        return None

    kissim_fp = load_kissim(structure_id)
    if kissim_fp is None:
        return None
    data = add_kissim_fp(data, kissim_fp, subset=PHYSICOCHEMICAL)

    data.y = torch.tensor(activity).view(1)
    data.docking_score = torch.tensor(docking_score).view(1)
    data.posit_prob = torch.tensor(posit_prob).view(1)
    data.activity_type = activity_type
    data.ident = ident
    data.smiles = smiles

    return data


if __name__ == "__main__":
    transforms = [AddDistances(("pocket", "bond", "pocket"))]
    dataset = KinodataDocked(transform=Compose(transforms))
    print(dataset[3])

    pass
