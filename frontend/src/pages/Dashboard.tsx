import { Chart }      from '../components/Chart'
import { PriceTable } from '../components/PriceTable'

interface Props {
  symbol: string
}

export function Dashboard({ symbol }: Props) {
  return (
    <div className="dashboard-col">
      <Chart symbol={symbol} />
      <PriceTable symbol={symbol} />
    </div>
  )
}
