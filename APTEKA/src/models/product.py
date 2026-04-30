from dataclasses import dataclass, field


@dataclass
class Product:
    guid: str
    barcode: str
    name: str
    pack: int
    rest_abs: int  # absolute stock (units)
    rest_rezerv: int  # reserved stock
    price: float  # retail price
    nds: int  # 1 = with VAT, 0 = without

    producer: str = ""
    expiration_date: str = ""
    price_buy: float = 0.0
    price_limit: float = 0.0
    reg_num: str = ""
    margin: str = ""
    series: str = ""
    tnvd: str = ""
    note: str = ""
    groups: str = ""
    rest_pack: int = 0
    rest_piece: int = 0
    nds_vat: int = 0
    no_discount: int = 0

    @property
    def is_prescription(self) -> bool:
        groups_lower = self.groups.lower()
        return "рецептурн" in groups_lower

    @property
    def in_stock(self) -> bool:
        return self.available_qty > 0

    @property
    def available_qty(self) -> int:
        return max(0, self.rest_abs - self.rest_rezerv)

    @property
    def group_list(self) -> list[str]:
        if not self.groups:
            return []
        return [g.strip() for g in self.groups.split(";") if g.strip()]

    def short_info(self) -> str:
        status = "В наличии" if self.in_stock else "Нет в наличии"
        qty = f"{self.available_qty} шт." if self.in_stock else ""
        prescription = " [РЕЦЕПТУРНЫЙ]" if self.is_prescription else ""
        price_str = f"{self.price:,.0f} тг".replace(",", " ")
        parts = [
            f"{self.name}{prescription}",
            f"Цена: {price_str}",
            f"Статус: {status} {qty}".strip(),
        ]
        if self.producer:
            parts.append(f"Производитель: {self.producer}")
        return "\n".join(parts)

    @classmethod
    def from_api(cls, data: dict) -> "Product":
        return cls(
            guid=data.get("guid", ""),
            barcode=data.get("barcode", ""),
            name=data.get("name", "").strip(),
            pack=int(data.get("pack", 1)),
            rest_abs=int(data.get("rest_abs", 0)),
            rest_rezerv=int(data.get("rest_rezerv", 0)),
            price=float(data.get("price", 0)),
            nds=int(data.get("nds", 0)),
            producer=data.get("producer", "").strip(),
            expiration_date=data.get("expiration_date", ""),
            price_buy=float(data.get("price_buy", 0)),
            price_limit=float(data.get("price_limit", 0) or 0),
            reg_num=data.get("reg_num", ""),
            margin=data.get("margin", ""),
            series=data.get("series", ""),
            tnvd=data.get("tnvd", ""),
            note=data.get("note", ""),
            groups=data.get("groups", ""),
            rest_pack=int(data.get("rest_pack", 0)),
            rest_piece=int(data.get("rest_piece", 0)),
            nds_vat=int(data.get("nds_vat", 0)),
            no_discount=int(data.get("no_discount", 0)),
        )
