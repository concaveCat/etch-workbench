// The standalone probe never invokes VTK output. ViennaPS 4.6.2 nevertheless
// names this optional template in an unused Domain member when VTK is off.
namespace viennals { template <class T, int D> class WriteVisualizationMesh; }

#include <geometries/psMakePlane.hpp>
#include <models/psMultiParticleProcess.hpp>
#include <process/psProcess.hpp>
#include <psDomain.hpp>
#include <vcContext.hpp>

#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <vector>
#include <omp.h>

using namespace viennaps;
using viennacore::SmartPointer;

template <int D, typename Model>
int run(const std::string &variant, const std::string &output) {
  auto domain = [&]() {
    if constexpr (D == 2)
      return SmartPointer<Domain<double, D>>::New(0.2, 4.0, BoundaryType::REFLECTIVE_BOUNDARY);
    else
      return SmartPointer<Domain<double, D>>::New(0.2, 4.0, 4.0, BoundaryType::REFLECTIVE_BOUNDARY);
  }();
  MakePlane<double, D>(domain, 0.0, Material::Si, false).apply();
  auto model = SmartPointer<Model>::New();
  const double argonThreshold = variant == "suppress-argon" ? 10000.0 : 10.0;
  const double xenonThreshold = variant == "suppress-xenon" ? 10000.0 : 10.0;
  model->addIonParticle(1., 0., 50., 60., -1., 120., 15., argonThreshold, 25., 1., "argonIon");
  if (variant == "empty-map") {
    if constexpr (std::is_same_v<Model, gpu::MultiParticleProcess<double, D>>)
      model->addNeutralParticle(MaterialValueMap<double>{}, 0.4, "fluorineNeutral");
    else
      model->addNeutralParticle(std::unordered_map<Material, double>{}, 0.4, "fluorineNeutral");
  } else {
    model->addNeutralParticle(0.4, "fluorineNeutral");
  }
  model->addIonParticle(1., 0., 50., 60., -1., 180., 15., xenonThreshold, 25., 1., "xenonIon");
  size_t callbacks = 0;
  model->setRateFunction([&](const std::vector<double> &fluxes, const Material &) {
    if (fluxes.size() != 3)
      throw std::runtime_error("native callback did not receive three species");
    ++callbacks;
    return -0.025 * std::accumulate(fluxes.begin(), fluxes.end(), 0.0);
  });
  Process<double, D> process(domain, model, 0.2);
  process.setFluxEngineType(FluxEngineType::GPU_DISK);
  process.setDeviceId(0);
  RayTracingParameters rays;
  rays.raysPerPoint = 128;
  rays.rngSeed = 314159;
  rays.useRandomSeeds = false;
  rays.maxBoundaryHits = 80;
  rays.maxReflections = 3;
  process.setParameters(rays);
  AdvectionParameters advection;
  advection.timeStepRatio = 0.25;
  process.setParameters(advection);
  auto mesh = process.calculateFlux();
  if (!mesh)
    throw std::runtime_error("native calculateFlux returned no mesh");
  const std::vector<std::string> labels = {"argonIon0", "fluorineNeutral1", "xenonIon2"};
  std::vector<double> means;
  for (const auto &label : labels) {
    auto *values = mesh->getCellData().getScalarData(label, true);
    if (!values || values->empty())
      throw std::runtime_error("native flux field missing: " + label);
    for (auto value : *values)
      if (!std::isfinite(value))
        throw std::runtime_error("non-finite native flux field: " + label);
    means.push_back(std::accumulate(values->begin(), values->end(), 0.0) / values->size());
  }
  process.apply();
  if (callbacks == 0)
    throw std::runtime_error("native process did not invoke the rate callback");
  std::ofstream file(output);
  if (!file)
    throw std::runtime_error("cannot create result file");
  file << std::setprecision(17) << "{\"status\":\"succeeded\",\"dimension\":" << D
       << ",\"variant\":\"" << variant << "\",\"engine\":\"GPU_DISK\",\"fields\":{"
       << "\"argonIon0\":" << means[0] << ",\"fluorineNeutral1\":" << means[1]
       << ",\"xenonIon2\":" << means[2] << "},\"callbacks\":" << callbacks << "}\n";
  return 0;
}

int main(int argc, char **argv) {
  if (argc != 6) {
    std::cerr << "usage: probe DIMENSION generic|gpu-specific VARIANT PTX_DIR OUTPUT\n";
    return 2;
  }
  try {
    omp_set_num_threads(1);
    auto context = viennacore::DeviceContext::createContext(argv[4], 0);
    if (!context || !context->foundCuda())
      throw std::runtime_error("CUDA context could not be created");
    const int dimension = std::stoi(argv[1]);
    const std::string route = argv[2], variant = argv[3];
    if (dimension == 2 && route == "generic")
      return run<2, MultiParticleProcess<double, 2>>(variant, argv[5]);
    if (dimension == 3 && route == "generic")
      return run<3, MultiParticleProcess<double, 3>>(variant, argv[5]);
    if (dimension == 2 && route == "gpu-specific")
      return run<2, gpu::MultiParticleProcess<double, 2>>(variant, argv[5]);
    if (dimension == 3 && route == "gpu-specific")
      return run<3, gpu::MultiParticleProcess<double, 3>>(variant, argv[5]);
    throw std::runtime_error("unknown dimension or native binding route");
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
