class SpatialReference:
    def ImportFromEPSG(self, code): self.code = code
    def ExportToWkt(self): return f"EPSG:{self.code}"
