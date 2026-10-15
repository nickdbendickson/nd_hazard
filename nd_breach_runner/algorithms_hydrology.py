"""Rainfall -> PMF inflow hydrograph tool of the provider."""
from qgis.core import (QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputNumber,
                       QgsProcessingOutputString, QgsProcessingParameterEnum,
                       QgsProcessingParameterFileDestination)

from .algorithms_tools import _ToolBase
from .core import hydrology

DISTRIBUTION_CHOICES = [("scs_type2", "SCS Type II (24-hr curve, stretched to the duration)"),
                        ("uniform", "Uniform intensity"),
                        ("triangular", "Triangular (peak position below)"),
                        ("user", "My own cumulative-fraction CSV")]
LOSS_CHOICES = [("cn", "SCS curve number"),
                ("constant", "Constant initial loss + constant loss rate"),
                ("none", "None (all rain runs off)")]
AMC_CHOICES = [("I", "I (dry)"), ("II", "II (average)"), ("III", "III (wet)")]
TC_CHOICES = [("user", "My time of concentration (hr)"),
              ("scs_lag", "SCS lag equation from flow length, slope and CN"),
              ("kirpich", "Kirpich from flow length and slope")]
UH_CHOICES = [("scs", "SCS dimensionless (lag = 0.6 tc)"),
              ("clark", "Clark (Tc and R)"),
              ("lag", "SCS shape with my own lag (hr)")]


class RainfallRunoffAlgorithm(_ToolBase):
    def name(self):
        return "rainfallrunoff"

    def displayName(self):
        return "Rainfall to inflow (PMF) hydrograph"

    def shortHelpString(self):
        return ("Builds the reservoir inflow hydrograph for the flood scenario from a storm: "
                "storm depth x temporal distribution, minus losses (SCS curve number, or a "
                "constant initial loss and rate, or none), convolved with a unit hydrograph "
                "(SCS dimensionless with an adjustable peak rate factor, Clark, or the SCS shape "
                "with your own lag), plus a constant base flow (snowmelt / antecedent flow). "
                "Writes the CSV (time hr, discharge cfs) that 'PMF inflow hydrograph CSV' "
                "reads; time zero is the start of the storm.\n\n"
                "The storm depth is YOUR input (PMP or a design storm for the chosen duration); "
                "no PMP value is generated. Use the contributing drainage area (leave out "
                "non-contributing pothole areas). The SCS Type II curve is defined for 24 hr. "
                "Time of concentration: your value, the SCS lag equation "
                "lag = l^0.8 (S+1)^0.7 / (1900 Y^0.5) hr with S = 1000/CN - 10 and tc = lag/0.6 "
                "(l ft, Y average watershed slope in %, AMC II CN), or Kirpich "
                "tc = 0.0078 L^0.77 S^-0.385 min (L ft, S ft/ft; use the main-channel slope). "
                "Both are small-basin formulas. The time step is tc/5, at most 1/96 of the storm "
                "(1-60 min, dividing the storm evenly); the hydrograph volume is checked against the excess rainfall "
                "(within 1 %) and warnings are listed for questionable inputs.")

    def initAlgorithm(self, config=None):
        self._num("AREA_MI2", "Contributing drainage area (mi2)", minimum=0.001)
        self._num("DEPTH_IN", "Storm depth for the duration (in): PMP or design storm, "
                  "supplied by you", minimum=0.001)
        self._num("DURATION_HR", "Storm duration (hr)", 24.0, minimum=0.05)
        self.addParameter(QgsProcessingParameterEnum(
            "DISTRIBUTION", "Temporal distribution", [c[1] for c in DISTRIBUTION_CHOICES],
            defaultValue=0))
        self._num("PEAK_POSITION", "Triangular: peak at this fraction of the storm (0.25 "
                  "front-loaded, 0.5 centre, 0.75 back-loaded)", 0.5, minimum=0.02,
                  maximum=0.98)
        self._file("DIST_CSV", "Own distribution: CSV of time (hr) and cumulative fraction "
                   "(0-1 or percent), from 0 to 1", "csv")
        self.addParameter(QgsProcessingParameterEnum(
            "LOSS_METHOD", "Losses", [c[1] for c in LOSS_CHOICES], defaultValue=0))
        self._num("CN", "Curve number (AMC II)", 80.0, minimum=1, maximum=100)
        self.addParameter(QgsProcessingParameterEnum(
            "AMC", "Antecedent moisture condition", [c[1] for c in AMC_CHOICES],
            defaultValue=1))
        self._num("IA_RATIO", "Initial abstraction ratio Ia/S", 0.2, minimum=0, maximum=0.5)
        self._num("INITIAL_LOSS_IN", "Constant losses: initial loss (in)", 0.0, minimum=0)
        self._num("LOSS_RATE", "Constant losses: loss rate (in/hr)", 0.0, minimum=0)
        self._num("BASE_FLOW_CFS", "Base flow (cfs), e.g. snowmelt or antecedent flow, added "
                  "over the whole record", 0.0, minimum=0)
        self.addParameter(QgsProcessingParameterEnum(
            "TC_METHOD", "Time of concentration", [c[1] for c in TC_CHOICES],
            defaultValue=0))
        self._num("TC_HR", "Your time of concentration (hr)", optional=True, minimum=0.001)
        self._num("FLOW_LENGTH_FT", "Flow length (ft): SCS lag or Kirpich", optional=True,
                  minimum=1)
        self._num("SLOPE_PCT", "Slope (%): average watershed slope for the SCS lag equation, "
                  "main-channel slope for Kirpich", optional=True, minimum=0.001)
        self.addParameter(QgsProcessingParameterEnum(
            "UH_METHOD", "Unit hydrograph", [c[1] for c in UH_CHOICES], defaultValue=0))
        self._num("PRF", "SCS peak rate factor (484 standard; usually 300-600)", 484.0,
                  minimum=50, maximum=1500)
        self._num("LAG_HR", "Own lag time (hr), for 'SCS shape with my own lag'",
                  optional=True, minimum=0.001)
        self._num("CLARK_TC_HR", "Clark Tc (hr; empty = the time of concentration)",
                  optional=True, minimum=0.001)
        self._num("CLARK_R_HR", "Clark storage coefficient R (hr)", optional=True,
                  minimum=0.001)
        self._num("DT_MIN", "Time step override (min; empty = tc/5)", optional=True,
                  minimum=0.01, advanced=True)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Inflow hydrograph CSV (time hr, discharge cfs)", "CSV files (*.csv)"))
        self.addOutput(QgsProcessingOutputNumber("PEAK_CFS", "Peak inflow (cfs)"))
        self.addOutput(QgsProcessingOutputNumber("VOLUME_ACFT", "Inflow volume (ac-ft)"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Summary (markdown)"))
        self.addOutput(QgsProcessingOutputFile("HYDROGRAPH", "Inflow hydrograph CSV"))

    def _settings(self, parameters, context):
        def enum(name, choices):
            i = self.parameterAsEnum(parameters, name, context)
            return choices[max(0, min(i, len(choices) - 1))][0]

        def num(name):
            return self.parameterAsDouble(parameters, name, context)

        def opt(name):
            return self._opt(parameters, name, context)

        return {
            "area_mi2": num("AREA_MI2"), "storm_depth_in": num("DEPTH_IN"),
            "duration_hr": num("DURATION_HR"),
            "distribution": enum("DISTRIBUTION", DISTRIBUTION_CHOICES),
            "peak_position": num("PEAK_POSITION"),
            "dist_csv": self._fileparam(parameters, "DIST_CSV", context) or None,
            "loss_method": enum("LOSS_METHOD", LOSS_CHOICES), "cn": num("CN"),
            "amc": enum("AMC", AMC_CHOICES), "ia_ratio": num("IA_RATIO"),
            "initial_loss_in": num("INITIAL_LOSS_IN"), "loss_rate_in_hr": num("LOSS_RATE"),
            "base_flow_cfs": num("BASE_FLOW_CFS"),
            "tc_method": enum("TC_METHOD", TC_CHOICES), "tc_hr": opt("TC_HR"),
            "flow_length_ft": opt("FLOW_LENGTH_FT"), "slope_pct": opt("SLOPE_PCT"),
            "uh_method": enum("UH_METHOD", UH_CHOICES), "prf": num("PRF"),
            "lag_hr": opt("LAG_HR"), "clark_tc_hr": opt("CLARK_TC_HR"),
            "clark_r_hr": opt("CLARK_R_HR"), "dt_min": opt("DT_MIN"),
        }

    def processAlgorithm(self, parameters, context, feedback):
        out = self._out(parameters, context)
        try:
            res = hydrology.hydrograph_from_settings(self._settings(parameters, context))
            hydrology.write_csv(res, out)
        except hydrology.HydrologyError as e:
            raise QgsProcessingException(str(e))
        except OSError as e:
            raise QgsProcessingException("Cannot write {}: {}".format(out, e))
        for k, v in res.summary.items():
            feedback.pushInfo("{}: {}".format(k, v))
        for w in res.warnings:
            feedback.pushWarning("Check: " + w)
        feedback.pushInfo("Inflow hydrograph: " + out)
        return {"OUTPUT": out, "HYDROGRAPH": out, "PEAK_CFS": res.peak_cfs,
                "VOLUME_ACFT": res.volume_acft, "RESULT": res.markdown}


HYDROLOGY_ALGORITHMS = (RainfallRunoffAlgorithm,)
