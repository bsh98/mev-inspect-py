from typing import Optional
from web3 import Web3
from mev_inspect.schemas.classified_traces import ClassifiedTrace
from mev_inspect.schemas.liquidations import (
    Liquidation,
    LiquidationType,
    LiquidationStatus,
    LiquidationCollateralSource,
)
from mev_inspect.schemas.blocks import Transaction
from mev_inspect.classifier_specs import Protocol

# from mev_inspect.tokenflow import get_dollar_flows, get_tx_proxies
from mev_inspect.historical_price import get_erc20_token_decimals
from mev_inspect.abi import get_raw_abi

w3 = Web3(Web3.HTTPProvider(""))

comp_v2_comptroller_address = "0x3d9819210A31b4961b30EF54bE2aeD79B9c9Cd3B"
c_ether = "0x4Ddc2D193948926D02f9B1fE9e1daa0718270ED5"

# cToken=>Token mapping (cDAI=>DAI)
# useful for finding out the underlying asset address of the cToken seized
def get_all_comp_markets():
    c_token_mapping = {}
    comp_v2_comptroller_abi = get_raw_abi("Comptroller", Protocol.compound_v2)
    comptroller_instance = w3.eth.contract(
        address=comp_v2_comptroller_address, abi=comp_v2_comptroller_abi
    )
    markets = comptroller_instance.functions.getAllMarkets().call()
    for c_token in markets:
        # make an exception for cETH (as it has no .underlying())
        if c_token != c_ether:
            comp_v2_ctoken_abi = get_raw_abi("CToken", Protocol.compound_v2)
            ctoken_instance = w3.eth.contract(address=c_token, abi=comp_v2_ctoken_abi)
            underlying_token = ctoken_instance.functions.underlying().call()
            c_token_mapping[
                c_token.lower()
            ] = underlying_token.lower()  # make k:v lowercase for consistancy
    return c_token_mapping


# find if the searcher repays the loan from their own EOA, by buying it from a DEX, or w/ a flashloan
# TODO: add all flashloan providers and their origin address
def find_collateral_source(
    classified_traces: list[ClassifiedTrace],
    tx: Transaction,
    liquidation_contract: Optional[str],
) -> LiquidationCollateralSource:
    source = LiquidationCollateralSource.other  # set other by default
    for classified_trace in classified_traces:
        # look for trace that liquidates and see from address
        if (
            classified_trace.to_address == liquidation_contract
            and classified_trace.function_name == "liquidateBorrow"
        ):
            ## check if tx originates from searcher eoa or contract
            if tx.to_address.lower() == classified_trace.from_address:
                source = LiquidationCollateralSource.searcher_contract
            elif tx.from_address.lower() == classified_trace.from_address:
                source = LiquidationCollateralSource.searcher_eoa
            ## flashloan providers identified here
    return source


def get_underlying_ctoken_exchange_rate(
    c_token_address: str, block_number: int
) -> float:
    comp_v2_ctoken_abi = get_raw_abi("CToken", Protocol.compound_v2)
    ctoken_instance = w3.eth.contract(address=c_token_address, abi=comp_v2_ctoken_abi)
    raw_exchange_rate = ctoken_instance.functions.exchangeRateCurrent().call(
        block_identifier=block_number
    )
    # format based on decimals in ctoken and the underlying token
    # see "Interpreting Exchange Rates" https://compound.finance/docs#protocol-math
    underlying_token_address = get_all_comp_markets()[c_token_address.lower()]
    decimals_in_underlying = get_erc20_token_decimals(
        Web3.toChecksumAddress(underlying_token_address)
    )
    decimals_in_ctoken = get_erc20_token_decimals(c_token_address)
    return raw_exchange_rate / (
        10 ** (18 + decimals_in_underlying - decimals_in_ctoken)
    )


def inspect_compound_v2_ceth(
    tx: Transaction, classified_traces: list[ClassifiedTrace]
) -> Liquidation:
    # flow:
    # 1. decide if it's a pre-flight check tx or an actual liquidation
    # 2. parse `liquidateBorrow` and `seize` sub traces to determine actual amounts
    # 3. calculate net profit by finding out the worth of seized tokens
    # 4. use tokenflow module to find out profit independent of the inspector, calculate diff
    # 5. prepare return object to get it ready for db processing
    for classified_trace in classified_traces:
        if (
            classified_trace.function_name == "liquidateBorrow"
            and classified_trace.inputs is not None
        ):
            source = find_collateral_source(
                classified_traces, tx, classified_trace.to_address
            )
            borrower = classified_trace.inputs["borrower"]
            c_token_collateral = classified_trace.inputs["cTokenCollateral"]
            liquidation = Liquidation(
                tx_hash=tx.tx_hash,
                borrower=borrower,
                collateral_provided="ether",
                collateral_provided_amount=classified_trace.value,
                asset_seized=(get_all_comp_markets())[c_token_collateral],
                asset_seized_amount=0,
                profit_in_eth=0,
                tokenflow_estimate_in_eth=0,
                tokenflow_diff=0,
                collateral_source=source,
                status=LiquidationStatus.seized,
                type=LiquidationType.compound_v2_ceth_liquidation,
            )
            return liquidation
    return Liquidation()
