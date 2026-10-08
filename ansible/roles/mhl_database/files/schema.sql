CREATE TABLE IF NOT EXISTS shipments (
  shipment_id integer PRIMARY KEY,
  customer_id integer NOT NULL,
  customer_name text NOT NULL,
  destination text NOT NULL,
  status text NOT NULL,
  invoice_total numeric(10,2) NOT NULL,
  dispatch_note text NOT NULL
);

INSERT INTO shipments (shipment_id, customer_id, customer_name, destination, status, invoice_total, dispatch_note)
VALUES
  (61001, 4101, 'Northwind Cold Storage', 'Duluth, MN', 'delayed', 18420.00, 'Refrigeration sensor reported intermittent restart.'),
  (61002, 4102, 'Cedar Freight Cooperative', 'Madison, WI', 'in_transit', 9730.50, 'Warehouse handoff scheduled after 18:00.'),
  (61003, 4101, 'Northwind Cold Storage', 'Green Bay, WI', 'delivered', 1280.00, 'Signed at receiving dock.')
ON CONFLICT (shipment_id) DO NOTHING;
